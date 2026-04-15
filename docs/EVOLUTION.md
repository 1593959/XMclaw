---
summary: "Autonomous evolution: Gene/Skill generation, validation, and hot reload"
read_when:
- Working on the evolution engine
- Understanding how Genes and Skills are created
- Debugging solidification or validation failures
title: "Evolution"
---

# Evolution

XMclaw's autonomous evolution system is its key differentiator. The agent learns from conversation, generates executable **Genes** (behavioral patterns) and **Skills** (tools), and continuously improves itself.

---

## The evolution loop (PCEC)

```
Conversation logs
        │
        ▼
   ┌─────────┐
   │ OBSERVE │  Pattern detection, intent stats, trend analysis
   └────┬────┘
        │
        ▼
   ┌─────────┐
   │  LEARN  │  Insight extraction, Gene/Skill design
   └────┬────┘
        │
        ▼
   ┌─────────┐
   │ EVOLVE  │  Code generation (GeneForge / SkillForge)
   └────┬────┘
        │
        ▼
   ┌─────────┐
   │ VALIDATE│  Compile + import + instantiate + execute
   └────┬────┘
        │
        ▼
   ┌─────────┐
   │ SOLIDIFY│  Register in GeneManager / ToolRegistry
   └────┬────┘
        │
        ▼
   ┌─────────┐
   │ RELOAD  │  Hot reload without restart
   └─────────┘
```

---

## Genes

### What is a Gene?

A Gene is an **abstract behavioral template**. It defines how the agent should adjust its system prompt or strategy in specific situations.

Examples:
- `gene_proactive_retrieval`: "Before searching the web, check long-term memory first."
- `gene_error_repair`: "When you encounter an error, attempt an automatic fix before asking the user."

### Gene structure

```python
class Gene:
    gene_id: str
    name: str
    description: str
    trigger: dict          # keywords, intents, regex patterns
    prompt_addition: str   # text injected into the system prompt
    priority: int          # higher = injected earlier
```

### Gene injection flow

1. User sends a message.
2. `GeneManager.match(user_input)` selects Genes whose triggers match.
3. `PromptBuilder` appends each matched Gene's `prompt_addition` to the system prompt.
4. The LLM naturally follows these behavioral instructions when generating a response.

---

## Skills

### What is a Skill?

A Skill is an **executable tool**. Unlike a Gene, a Skill directly extends what the agent can *do*.

Examples:
- `auto_entity_reference_v26`: Extracts file paths, URLs, and emails from user input.
- `auto_repair_v30`: Attempts automatic fixes based on cross-session error patterns.

### Skill generation flow

1. `SkillForge` analyzes insights (high-frequency needs, user pain points).
2. An LLM generates a complete Python `Tool` subclass.
3. `EvolutionValidator` runs four checks:
   - `py_compile` syntax check
   - `importlib` dynamic import
   - Instantiate the Tool subclass
   - Call `execute()` with sample arguments
4. If all checks pass, the file is saved to `shared/skills/skill_{name}.py`.
5. `ToolRegistry` hot-reloads the skill on the next lookup.

### Skill versioning

- Unlimited iterations are allowed; each version improves the previous one.
- Automatic cleanup keeps only the 2 most recent versions to prevent clutter.

---

## Triggers

Evolution does not run on every conversation. It triggers when:

1. **Conversation volume**: Enough new turns have accumulated.
2. **Time interval**: At least 30 minutes have passed since the last cycle.
3. **Pattern detection**: New user behavior patterns are observed.
4. **VFM threshold**: The generated artifact scores high enough on the Value Function Model.

---

## VFM (Value Function Model)

VFM decides whether an evolutionary artifact is worth keeping.

| Dimension | Weight | Description |
|-----------|--------|-------------|
| Novelty | 25% | Solves a previously uncovered problem |
| Generality | 25% | Applicable across multiple scenarios |
| Verifiability | 25% | Passes real execution validation |
| Simplicity | 25% | Clean, non-bloated implementation |

Artifacts with a total score ≥ 30/100 are solidified.

---

## Insights

The evolution engine extracts the following insight types from conversation logs:

- **High-frequency intents**: Repeated user requests for the same category of task.
- **Unmet needs**: Things the user asked for that the agent could not do.
- **Negative feedback**: Corrections, complaints, or failed expectations.
- **Success patterns**: Behaviors the user explicitly praised or accepted.

Insights are persisted to:
- `shared/memory.db` (`insights` table)
- `agents/{agent_id}/MEMORY.md` (long-term memory)

---

## Viewing evolution state

### Desktop app
Open the **Evolution** sidebar view to see Genes, Skills, and Insights.

### CLI
```bash
xmclaw evolution-status
```

### Web UI
Navigate to the **Evolution** panel in the Agent OS dashboard.

---

## Related

- [Architecture](./ARCHITECTURE.md) — Where the evolution engine sits in the system
- [Tools](./TOOLS.md) — How generated skills are loaded and executed
