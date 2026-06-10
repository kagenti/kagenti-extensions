#!/usr/bin/env python3
"""
AIAC CLI - Access Control Policy Generator

Command-line interface for generating Keycloak access control policies from
natural language descriptions using AI-powered semantic analysis.

Usage:
    python aiac_cli.py <policy_text_file> <config.yaml> <output.yaml>

Arguments:
    policy_text_file    Path to file containing natural language policy description
    config.yaml         Path to Keycloak realm configuration YAML
    output.yaml         Path where generated YAML policy will be saved

Example:
    python aiac_cli.py my_policy.txt keycloak_config.yaml generated_policy.yaml

The CLI generates a complete access control policy by:
    1. Reading the natural language policy description
    2. Loading Keycloak realm configuration (roles, clients)
    3. Using LLM to map roles based on semantic analysis
    4. Validating the generated policy structure
    5. Saving the result as a YAML file with explanatory comments

For programmatic usage, import PolicyBuilder directly:
    from full_policy_agent import PolicyBuilder
    builder = PolicyBuilder(config_path=Path("config.yaml"))
    result = builder.generate_policy("policy description")
"""

import argparse
import os
import sys
from pathlib import Path

# Add current directory to path to allow importing local modules
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

from full_policy_agent.graph import PolicyBuilder
from config import create_llm

load_dotenv(dotenv_path="aiac.env", override=True)


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"


def print_step(message: str) -> None:
    print(f"{Colors.BLUE}{'=' * 51}{Colors.NC}")
    print(f"{Colors.BLUE}{message}{Colors.NC}")
    print(f"{Colors.BLUE}{'=' * 51}{Colors.NC}")


def print_success(message: str) -> None:
    print(f"{Colors.GREEN}✓ {message}{Colors.NC}")


def print_error(message: str) -> None:
    print(f"{Colors.RED}✗ {message}{Colors.NC}")


def print_info(message: str) -> None:
    print(f"{Colors.YELLOW}ℹ {message}{Colors.NC}")


def generate_policy_only(
    policy_file: Path, config_path: Path, output_file: str
) -> None:
    """
    Args:
        policy_file: Path to file containing natural language policy description
        config_path: Path to Keycloak realm configuration YAML
        output_file: Path where generated YAML policy will be saved
    """
    if not policy_file.exists():
        raise FileNotFoundError(f"Policy file not found: {policy_file}")

    with open(policy_file, "r") as f:
        policy_text = f.read().strip()

    # Load default model name from llm_conf.yaml
    import yaml
    llm_models_path = Path(__file__).parent / "config" / "llm_conf.yaml"
    with open(llm_models_path) as f:
        llm_config = yaml.safe_load(f)
    default_model = llm_config.get("default_model", "gpt-oss")
    
    # Create LLM instance from llm_models.yaml using default model
    llm = create_llm(model_name=default_model, verbose=False)
    
    # Create PolicyBuilder with the LLM instance
    builder = PolicyBuilder(config_path=config_path, llm=llm)

    print("=" * 80)
    print("Generating access rule from textual policy...")
    print("=" * 80)
    print(f"\nPolicy file: {policy_file}")
    print(f"\nDescription:\n{policy_text}\n")

    result = builder.generate_policy(description=policy_text)

    if result["success"]:
        print("✓ Access rules generated successfully!\n")
        print("Generated YAML:")
        print("-" * 80)
        print(result["yaml_output"])
        print("-" * 80)
        builder.save_policy(result["yaml_output"], output_file)
    else:
        print("✗ Policy generation failed with errors:")
        for error in result["errors"]:
            print(f"  - {error}")

    print("\n" + "=" * 80)
    print("Parsed Role-to-Privilege Mappings:")
    print("=" * 80)
    for role_mapping in result["parsed_scopes"]:
        realm_role = role_mapping["role"]
        privileges = role_mapping.get("privileges", [])
        print(f"  {realm_role}:")
        for priv in privileges:
            print(f"    - {priv['service']}: {priv['privilege']}")

def main() -> None:
    gen_parser = argparse.ArgumentParser(
        prog="aiac generate",
        description="Run only the policy generation step (no Keycloak).",
    )
    gen_parser.add_argument("policy_file", help="Path to natural-language policy description")
    gen_parser.add_argument("config", help="Path to realm config YAML")
    gen_parser.add_argument("output", help="Output YAML path")
    gen_args = gen_parser.parse_args(sys.argv[2:])
    generate_policy_only(
        Path(gen_args.policy_file), Path(gen_args.config), gen_args.output
    )
    return

if __name__ == "__main__":
    main()
