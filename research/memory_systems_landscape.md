# Phase 1: Landscape Scan — AI Agent Memory Systems

> Date: 2026-06-06  
> Route: B (Focused Search)  
> Scope: Mainstream memory system implementations and application scenarios with paper support

---

## Key Findings

### 1. Academic Lineage

The field traces back to two landmark papers:

- **Generative Agents** (Park et al., 2023, UIST) [^park2023] — introduced the "memory stream" pattern: raw experience capture → periodic reflection synthesis → higher-level abstractions. This is the structural precursor to virtually all modern agent memory systems.
- **MemGPT** (Packer et al., 2023) [^packer2023] — formalized LLM memory as *virtual context management* borrowed from OS virtual memory paging. The LLM manages what sits in its context window via function calls, moving data between main context and external archival storage.

### 2. Major Survey Papers (2024–2025)

- **"A Survey on the Memory Mechanism of Large Language Model Based Agents"** (Zhang et al., 2024, arXiv:2404.13501) [^zhang2024] — early comprehensive taxonomy.
- **"Memory in the Age of AI Agents: Forms, Functions and Dynamics"** (Hu et al., 2025, arXiv:2512.13564) [^hu2025] — unified taxonomy through three lenses: Forms (Token/Parametric/Latent), Functions (Factual/Experiential/Working), Dynamics (Formation/Evolution/Retrieval).
- **"Agentic AI: Autonomous Intelligence for Complex Goals"** (Acharya et al., 2025, IEEE Access) [^acharya2025] — includes memory as a core agentic primitive.

### 3. Key Industrial Systems

| System | Architecture | Key Paper | Distinctive Feature |
|--------|-------------|-----------|---------------------|
| **Mem0** | Dual-store (Vector + Graph) | Chhikara et al., 2025 [^chhikara2025] | Mem0-style write-time decision (ADD/UPDATE/DELETE/NOOP); enterprise SOC2/HIPAA |
| **Zep (Graphiti)** | Temporal Knowledge Graph | Rasmussen et al., 2025 [^rasmussen2025] | Bi-temporal modeling (event time vs transaction time); LongMemEval benchmark leader |
| **MemGPT / Letta** | Virtual context paging | Packer et al., 2023 [^packer2023] | OS-inspired context management; explicit memory tools |
| **LangMem** | LangGraph native | — | Free, Python-only, no managed semantic search |
| **Hindsight** | 4-strategy hybrid | — | Semantic + BM25 + Graph + Temporal; MIT license; LongMemEval 91.4% |
| **Cognee** | Knowledge graph builder | — | Graph-native; open-source |

### 4. Retrieval Architecture Trends

- **Pure Vector** → **Hybrid (Vector + BM25)** → **Graph-Enhanced** → **Multi-Strategy Fusion**
- Reciprocal Rank Fusion (RRF) with `k=60` is the dominant fusion algorithm [^rrf]
- LanceDB, Qdrant, Chroma, Neo4j AuraDB are common backends

### 5. Evaluation Landscape

- **LongMemEval** — enterprise-focused, complex temporal reasoning [^longmemeval]
- **DMR (Deep Memory Retrieval)** — MemGPT-era benchmark [^dmr]
- **MemTrack** (Deshpande et al., 2025) — multi-platform dynamic agent environments [^deshpande2025]
- **HaluMem** (Chen et al., 2025) — hallucination evaluation in memory systems [^chen2025halu]
- **Forgetting Curve** (2024) — measures inherent long-context memorization capability [^forgetting2024]

### 6. Gaps Requiring Deep Investigation

- How do different architectures (vector-only vs graph-only vs hybrid) perform on the same benchmark?
- What is the exact write-path decision logic in Mem0 vs Zep vs academic prototypes?
- How do temporal reasoning capabilities differ across systems?
- What are the concrete application scenarios with quantitative results?
- Security: memory contamination, adversarial injection, sleeper memory poisoning

---

## Citations

[^park2023]: Park, J. S., O'Brien, J. C., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S. (2023). Generative Agents: Interactive Simulacra of Human Behavior. UIST 2023. arXiv:2304.03442.

[^packer2023]: Packer, C., Wooders, S., Lin, K., Fang, V., Patil, S. G., Stoica, I., & Gonzalez, J. E. (2023). MemGPT: Towards LLMs as Operating Systems. arXiv:2310.08560.

[^zhang2024]: Zhang, Z., Bo, X., Ma, C., Li, R., Chen, X., Dai, Q., Zhu, J., Dong, Z., & Wen, J. R. (2024). A survey on the memory mechanism of large language model based agents. arXiv:2404.13501.

[^hu2025]: Hu, Y., et al. (2025). Memory in the Age of AI Agents: Forms, Functions and Dynamics. arXiv:2512.13564.

[^acharya2025]: Acharya, D. B., Kuppan, K., & Divya, B. (2025). Agentic AI: Autonomous Intelligence for Complex Goals — A Comprehensive Survey. IEEE Access.

[^chhikara2025]: Chhikara, P., Khant, D., Aryan, S., Singh, T., & Yadav, D. (2025). Mem0: Building production-ready AI agents with scalable long-term memory. arXiv:2504.19413.

[^rasmussen2025]: Rasmussen, P., Paliychuk, P., Beauvais, T., Ryan, J., & Chalef, D. (2025). Zep: A Temporal Knowledge Graph Architecture for Agent Memory. arXiv:2501.13956.

[^deshpande2025]: Deshpande, D., Gangal, V., Mehta, H., Kannappan, A., Qian, R., & Wang, P. (2025). MemTrack: Evaluating long-term memory and state tracking in multi-platform dynamic agent environments. arXiv:2510.01353.

[^chen2025halu]: Chen, D., Niu, S., Li, K., Liu, P., Zheng, X., Tang, B., Li, X., Xiong, F., & Li, Z. (2025). HaluMem: Evaluating hallucinations in memory systems of agents. arXiv:2511.03506.

[^forgetting2024]: (2024). Forgetting Curve: A Reliable Method for Evaluating Memorization Capability for Long-context Models. arXiv:2410.04727.

[^rrf]: LearnWithParam (2026). Hybrid retrieval in RAG: vector + graph search. https://www.learnwithparam.com/blog/hybrid-retrieval-rag-vector-graph-search

[^longmemeval]: Vectorize.io (2026). LongMemEval Benchmark. Referenced in multiple 2026 comparisons.

[^dmr]: Deep Memory Retrieval benchmark. Referenced in MemGPT and Zep papers.
