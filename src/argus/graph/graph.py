"""Full Argus review graph — assembles all nodes and edges.

Topology:
  input_guardrail
       ↓
   supervisor
     ↙↓↓↓↙
  [workers in parallel via Send()]
       ↓
   aggregator
       ↓ (conditional)
  ┌─ gate_critical_triage (if critical findings)
  │        ↓ (conditional)
  └──► gate_final_approval
              ↓ (conditional)
        report_generator
              ↓
           __end__
"""

from __future__ import annotations

from functools import partial

from langgraph.graph import END, StateGraph
from langgraph.types import Send

from argus.agents.aggregator import AggregatorAgent
from argus.agents.code_quality.agent import CodeQualityAgent
from argus.agents.documentation.agent import DocumentationAgent
from argus.agents.logic.agent import LogicAgent
from argus.agents.report_generator import ReportGeneratorAgent
from argus.agents.security.supervisor import SecuritySupervisor
from argus.agents.static_analysis.agent import StaticAnalysisAgent
from argus.config import get_settings
from argus.graph.nodes.aggregator import (
    aggregator_node,
    route_after_aggregation,
    route_after_critical_triage,
    route_after_final_approval,
)
from argus.graph.nodes.guardrail import input_guardrail_node
from argus.graph.nodes.hitl import gate_critical_triage_node, gate_final_approval_node
from argus.graph.nodes.report import report_generator_node
from argus.graph.nodes.supervisor import supervisor_node
from argus.graph.nodes.workers import _make_worker_node
from argus.graph.state import ReviewState
from argus.llm.router import LLMRouter


def _route_after_guardrail(state: ReviewState) -> str:
    if state.get("status") == "failed":
        return END
    return "supervisor"


def _fan_out_workers(state: ReviewState) -> list[Send]:
    """Called by LangGraph to generate parallel Send() calls for workers."""
    plan = state.get("plan")
    if not plan:
        return [Send("aggregator_node", state)]
    return [Send(f"{w}_node", state) for w in plan.workers]


def build_graph(router: LLMRouter | None = None) -> StateGraph:
    """Assemble the full review graph. Returns a compiled graph."""
    settings = get_settings()

    # --- Instantiate agents ---
    static_agent = StaticAnalysisAgent()
    security_agent = SecuritySupervisor(router=router)
    logic_agent = LogicAgent(router=router)
    quality_agent = CodeQualityAgent(router=router, complexity_threshold=settings.complexity_threshold)
    doc_agent = DocumentationAgent(router=router)
    aggregator_agent = AggregatorAgent(router=router, max_iterations=settings.max_refine_iterations)
    report_agent = ReportGeneratorAgent(router=router)

    # --- Worker node map (name → async function) ---
    worker_agents = {
        "static_analysis": static_agent,
        "security_supervisor": security_agent,
        "logic_correctness": logic_agent,
        "code_quality": quality_agent,
        "documentation": doc_agent,
    }

    # --- Build graph ---
    builder = StateGraph(ReviewState)

    # Fixed nodes
    builder.add_node("input_guardrail", input_guardrail_node)
    builder.add_node("supervisor", supervisor_node)

    # Worker nodes (one per agent)
    for name, agent in worker_agents.items():
        builder.add_node(f"{name}_node", _make_worker_node(agent))

    # Aggregator (needs agent instance via partial)
    builder.add_node(
        "aggregator_node",
        partial(aggregator_node, agent=aggregator_agent),
    )

    # HITL gates
    builder.add_node("gate_critical_triage", gate_critical_triage_node)
    builder.add_node("gate_final_approval", gate_final_approval_node)

    # Report
    builder.add_node(
        "report_generator",
        partial(report_generator_node, agent=report_agent),
    )

    # --- Edges ---
    builder.set_entry_point("input_guardrail")

    builder.add_conditional_edges(
        "input_guardrail",
        _route_after_guardrail,
        {"supervisor": "supervisor", END: END},
    )

    # Supervisor fans out to workers in parallel
    builder.add_conditional_edges(
        "supervisor",
        _fan_out_workers,
        {f"{name}_node": f"{name}_node" for name in worker_agents},
    )

    # All workers converge on aggregator
    for name in worker_agents:
        builder.add_edge(f"{name}_node", "aggregator_node")

    # Post-aggregation routing
    builder.add_conditional_edges(
        "aggregator_node",
        route_after_aggregation,
        {
            "gate_critical_triage": "gate_critical_triage",
            "gate_final_approval": "gate_final_approval",
        },
    )

    builder.add_conditional_edges(
        "gate_critical_triage",
        route_after_critical_triage,
        {
            "gate_final_approval": "gate_final_approval",
            "__end__": END,
        },
    )

    builder.add_conditional_edges(
        "gate_final_approval",
        route_after_final_approval,
        {
            "report_generator": "report_generator",
            "__end__": END,
        },
    )

    builder.add_edge("report_generator", END)

    return builder


def compile_graph(router: LLMRouter | None = None, checkpointer=None):
    """Build and compile the graph, optionally with a checkpointer."""
    builder = build_graph(router=router)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["gate_critical_triage", "gate_final_approval"],
    )
