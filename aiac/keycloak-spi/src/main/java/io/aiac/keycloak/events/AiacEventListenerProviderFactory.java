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
        return new AiacEventListenerProvider(natsConnection);
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
        try {
            natsConnection = Nats.connect(natsUrl);
        } catch (IOException | InterruptedException e) {
            // Never fail Keycloak startup over a missing/unreachable Event Broker — the provider
            // tolerates a null connection and drops events with a warning instead.
            log.warnf(e, "could not connect to NATS at %s; %s will drop events until this is fixed", natsUrl,
                    PROVIDER_ID);
        }
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
