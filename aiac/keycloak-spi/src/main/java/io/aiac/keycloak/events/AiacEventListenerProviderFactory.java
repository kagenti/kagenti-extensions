package io.aiac.keycloak.events;

import io.nats.client.Connection;
import io.nats.client.Nats;
import org.jboss.logging.Logger;
import org.keycloak.Config;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventListenerProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;

import java.io.IOException;

/**
 * Keycloak factories are singletons; providers are created per-request. The NATS connection is
 * opened once here ({@link #postInit}) and shared across every {@link AiacEventListenerProvider}
 * instance this factory creates — never opened per request.
 */
public class AiacEventListenerProviderFactory implements EventListenerProviderFactory {

    public static final String PROVIDER_ID = "aiac-event-listener";
    private static final String DEFAULT_NATS_URL = "nats://aiac-event-broker-service:4222";

    private static final Logger log = Logger.getLogger(AiacEventListenerProviderFactory.class);

    private volatile String natsUrl;
    private volatile Connection natsConnection;

    @Override
    public String getId() {
        return PROVIDER_ID;
    }

    @Override
    public EventListenerProvider create(KeycloakSession session) {
        return new AiacEventListenerProvider(connection());
    }

    @Override
    public void init(Config.Scope config) {
        // SPI config value ("natsUrl") takes precedence, then the NATS_URL env var, then the
        // cluster default. Wiring either into the live Keycloak pod is a separate deployment's
        // job (see keycloak-spi/README.md) — this code is ready for it either way.
        natsUrl = config.get("natsUrl", System.getenv().getOrDefault("NATS_URL", DEFAULT_NATS_URL));
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        // Best-effort — never fail Keycloak startup over a missing/unreachable Event Broker.
        // If this fails, connection() retries on the next request instead of leaving every
        // future provider stuck with a permanently null connection.
        connection();
    }

    /**
     * Retry-on-use: {@link #postInit} can run before the Event Broker is reachable, and the
     * client itself moves to {@code CLOSED} once its own reconnect budget is exhausted — either
     * way {@code natsConnection} can go dead for good, and without this every later provider
     * would keep getting that dead connection and silently drop events until Keycloak is
     * restarted. Providers are created per request, so that's a natural, bounded retry point;
     * no background thread needed.
     */
    private synchronized Connection connection() {
        if (natsConnection != null && natsConnection.getStatus() != Connection.Status.CLOSED) {
            return natsConnection;
        }
        try {
            natsConnection = Nats.connect(natsUrl);
        } catch (IOException e) {
            log.warnf(e, "could not connect to NATS at %s; %s will drop this event", natsUrl, PROVIDER_ID);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            log.warnf(e, "interrupted while connecting to NATS at %s; %s will drop this event", natsUrl,
                    PROVIDER_ID);
        }
        return natsConnection;
    }

    @Override
    public void close() {
        if (natsConnection != null) {
            try {
                natsConnection.close();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                log.warn("interrupted while closing NATS connection", e);
            }
        }
    }
}
