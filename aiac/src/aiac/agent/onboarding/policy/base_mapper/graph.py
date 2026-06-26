#!/usr/bin/env python3
"""
Shared utilities and base class for single-item mapper agents.

Both SinglePrivilegeMapper and SingleRoleMapper follow the same
analyze → validate → verify_semantic LangGraph pattern. This module
provides the common building blocks so the subclasses only implement
what differs (prompt building, state field names, rule assembly).
"""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph

from aiac.pdp.policy.models import PolicyObjectModel, Rule
from base_mapper.state import BaseMappingState
from config.constants import MAX_VALIDATION_RETRIES


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class MapperConfig:
    """
    Shared configuration for single-item mapper agents.

    Attributes:
        llm: LangChain LLM instance
        verbose: Whether to print detailed output
        max_retries: Maximum validation retry attempts
    """
    llm: BaseChatModel
    verbose: bool = True
    max_retries: int = MAX_VALIDATION_RETRIES


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def extract_explanation_and_json(
    content: str,
) -> tuple[str, Optional[dict[str, Any]]]:
    """
    Extract explanation and JSON from an LLM response.

    Tries multiple parsing strategies in order:
    1. Fenced ```explanation and ```json blocks (preferred format)
    2. Any ```json or generic ``` block containing a dict
    3. A bare { ... } JSON object anywhere in the response

    Returns:
        Tuple of (explanation_text, parsed_json_dict).
        Returns ("", None) if parsing fails entirely.
    """
    explanation = ""
    json_data = None

    if "```explanation" in content:
        start = content.find("```explanation") + len("```explanation")
        end = content.find("```", start)
        if end != -1:
            explanation = content[start:end].strip()

    if "```json" in content:
        start = content.find("```json") + len("```json")
        end = content.find("```", start)
        if end != -1:
            try:
                json_data = json.loads(content[start:end].strip())
            except json.JSONDecodeError:
                pass

    if json_data is None and "```" in content:
        for block in re.findall(r"```[^\n]*\n(.*?)```", content, re.DOTALL):
            try:
                candidate = json.loads(block.strip())
                if isinstance(candidate, dict):
                    json_data = candidate
                    break
            except json.JSONDecodeError:
                pass

    if json_data is None:
        depth = 0
        start_idx = None
        for i, ch in enumerate(content):
            if ch == "{":
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start_idx is not None:
                    try:
                        candidate = json.loads(content[start_idx : i + 1])
                        if isinstance(candidate, dict):
                            json_data = candidate
                            if not explanation:
                                explanation = content[:start_idx].strip()
                            break
                    except json.JSONDecodeError:
                        start_idx = None

    return explanation, json_data


def print_explanation(
    explanation: str, is_retry: bool = False, verbose: bool = True
) -> None:
    """Print the LLM's explanation when verbose mode is on."""
    if verbose and explanation:
        prefix = "Retry Explanation:" if is_retry else "LLM Explanation:"
        print(f"\n{prefix}")
        print(explanation)
        print()


def validate_mapping_items(
    state: BaseMappingState,
    verbose: bool,
    max_retries: int,
    items_key: str,
    reference_key: str,
    item_type_label: str,
) -> dict[str, Any]:
    """
    Structural validation shared by both mapper agents.

    Checks that every item in state[items_key] exists in state[reference_key]
    and that there are no duplicates.

    Args:
        state: Current mapping state dict
        verbose: Whether to print validation errors
        max_retries: Maximum retry attempts
        items_key: State key holding the mapped items (e.g. "roles_with_access")
        reference_key: State key holding the full reference list (e.g. "roles")
        item_type_label: Human-readable label for error messages (e.g. "role")

    Returns:
        Updated state dict with errors and validation_passed fields set.
    """
    retry_count = state.get("retry_count", 0)
    items = state.get(items_key, [])
    available_names: set[str] = {r.name for r in state[reference_key]}

    errors: list[str] = []

    for item in items:
        if item.name not in available_names:
            errors.append(
                f"Unknown {item_type_label} '{item.name}'. "
                f"Must be one of: {', '.join(sorted(available_names))}"
            )

    if len(items) != len({item.name for item in items}):
        errors.append(f"Duplicate {item_type_label} names found in the result")

    validation_passed = len(errors) == 0

    if errors and retry_count < max_retries:
        if verbose:
            print(f"\n⚠️  Validation failed (attempt {retry_count + 1}/{max_retries})")
            for error in errors:
                print(f"  - {error}")
        return {
            **state,
            items_key: items,
            "errors": errors,
            "validation_passed": False,
            "retry_count": retry_count + 1,
        }

    return {
        **state,
        items_key: items,
        "errors": errors,
        "validation_passed": validation_passed,
        "retry_count": retry_count,
    }


def verify_semantic_mapping(
    state: BaseMappingState,
    llm: BaseChatModel,
    verbose: bool,
    max_retries: int,
    subject_name: str,
    verification_prompt: str,
    mapped_items: list,
) -> dict[str, Any]:
    """
    LLM semantic verification shared by both mapper agents.

    Invokes the LLM with a pre-built verification_prompt and parses the
    MAPPING_CORRECT: YES/NO response. On failure, increments retry_count so
    the graph can loop back to the analyze node.

    Args:
        state: Current mapping state dict
        llm: LLM instance for verification
        verbose: Whether to print verification details
        max_retries: Maximum retry attempts allowed
        subject_name: Display name of the item being verified (for logging)
        verification_prompt: Fully-built prompt to send to the LLM
        mapped_items: The items that were mapped (used to decide whether to log)

    Returns:
        Updated state dict with validation_passed and errors set.
    """
    retry_count = state.get("retry_count", 0)

    try:
        response = llm.invoke([HumanMessage(content=verification_prompt)])
        content = (
            response.content if isinstance(response.content, str) else str(response.content)
        )

        mapping_match = re.search(r"MAPPING_CORRECT:\s*(YES|NO)", content, re.IGNORECASE)
        explanation_match = re.search(
            r"EXPLANATION:\s*(.+?)$", content, re.DOTALL | re.IGNORECASE
        )

        mapping_correct = mapping_match.group(1).upper() == "YES" if mapping_match else False
        explanation = explanation_match.group(1).strip() if explanation_match else content

        if verbose and (mapped_items or not mapping_correct):
            status = "YES" if mapping_correct else "NO"
            print(f"\nSemantic verification [{subject_name}]: MAPPING_CORRECT={status}")
            if not mapping_correct:
                print(f"  {explanation}")

        if not mapping_correct:
            error_msg = f"Semantic mismatch for '{subject_name}': {explanation}"
            if retry_count < max_retries:
                return {
                    **state,
                    "errors": [error_msg],
                    "validation_passed": False,
                    "retry_count": retry_count + 1,
                }
            return {**state, "errors": [error_msg], "validation_passed": False}

        return {**state, "errors": [], "validation_passed": True}

    except Exception:
        # Allow the pipeline to proceed on transient errors (rate limits, etc.)
        return {**state, "errors": [], "validation_passed": True}


def should_route_after_structural_validation(
    validation_passed: bool,
    retry_count: int,
    max_retries: int,
    analyze_node: str,
    verify_node: str,
) -> str:
    """
    Shared routing logic after structural validation.

    Returns the analyze node name if retries remain, the verify node name if
    validation passed, or END if retries are exhausted.
    """
    if not validation_passed and retry_count < max_retries:
        return analyze_node
    if validation_passed:
        return verify_node
    return END


def should_retry_after_semantic(
    validation_passed: bool,
    retry_count: int,
    max_retries: int,
    analyze_node: str,
) -> str:
    """
    Shared routing logic after semantic verification.

    Returns the analyze node name if semantic check failed and retries remain,
    otherwise END.
    """
    if not validation_passed and retry_count < max_retries:
        return analyze_node
    return END


# ============================================================================
# BASE CLASS
# ============================================================================

class BaseSingleMapper(ABC):
    """
    Abstract base class for single-item mapper agents (privilege→roles and role→privileges).

    Subclasses implement _create_graph, _run, and _build_rules.
    generate_policy is fully implemented here using those hooks.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES,
    ) -> None:
        self.config = MapperConfig(llm=llm, verbose=verbose, max_retries=max_retries)
        self.graph = self._create_graph(self.config)

    @abstractmethod
    def _create_graph(self, config: MapperConfig) -> CompiledStateGraph:
        """Build and return the compiled LangGraph workflow."""
        ...

    def get_graph(self):
        """Return the compiled graph for visualization or inspection."""
        return self.graph

    @abstractmethod
    def _run(self, policy_description: str) -> dict[str, Any]:
        """Run the workflow and return a result dict with errors, explanation, etc."""
        ...

    @abstractmethod
    def _build_rules(self, result: dict[str, Any]) -> list[Rule]:
        """Convert the workflow result into a list of Rule objects."""
        ...

    def generate_policy(self, description: str) -> PolicyObjectModel:
        """
        Generate an access control policy from a natural language description.

        Args:
            description: Natural language policy description

        Returns:
            PolicyObjectModel with the generated rules and explanation.

        Raises:
            ValueError: If validation fails after all retries.
        """
        result = self._run(policy_description=description)
        errors = result.get("errors", [])
        if errors:
            raise ValueError(f"Policy validation failed: {'; '.join(errors)}")
        rules = self._build_rules(result)
        return PolicyObjectModel(rules=rules, explanation=result.get("explanation", ""))
