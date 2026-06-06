"""Comprehensive script to add ErrorAggregator.record() to ALL except Exception blocks."""
import re
from pathlib import Path

SEVERITY_RULES = [
    # CRITICAL: LLM / tool system failures
    (r"tool\.invoke_uncaught|retry.*raised uncaught|llm_retry|llm.*fail|B-227|B-230.*auto-continue", "CRITICAL"),
    (r"outer.*LLM|_run_hop_loop.*except.*Exception.*exc", "CRITICAL"),
    (r"invoke_uncaught|TimeoutError.*LLM|provider.*fail|LLM.*provider", "CRITICAL"),
    # WARNING: memory, cognitive, skill, grader
    (r"memory.*fail|recall.*fail|render_fail|extract_fail|unified_recall|memory_v2|memory_graph", "WARNING"),
    (r"cognitive.*fail|pop_proposals|salience|step_validator|HonestGrader|grader|skill.*prefilter|select_relevant_skills", "WARNING"),
    (r"context_engine.*fail|session_store|bootstrap|assemble", "WARNING"),
    (r"swarm_fanout_failed|auto_recall|relevant_files_picker", "WARNING"),
    # INFO: analytics, git, styles, compression, observational
    (r"analytics|cache_metrics|CacheMetrics|git_status|output_styles|platform_guidance|feature_flags", "INFO"),
    (r"compression|compress|narration|progress_marker|on_stream_fallback|step.*validator.*debug", "INFO"),
    (r"perception.*observational|hook.*fail|pre_tool_use|post_tool_use|evolution_loop|bg_extract", "INFO"),
    (r"trivial|correction_detector|mode_router|tier.*decision|strategy.*retrieve|plan_first", "INFO"),
    (r"extract_recent_paths|semantic_scores|skill_registry|steps_warrant|set_plan_mode", "INFO"),
    (r"prompt_injection.*publish|TODO_UPDATED|COST_TICK|INNER_MONOLOGUE.*publish", "INFO"),
    (r"best-effort|nice-to-have|observational|fire-and-forget", "INFO"),
]

def get_severity(context: str) -> str:
    ctx = context.lower()
    for pattern, sev in SEVERITY_RULES:
        if re.search(pattern, ctx, re.IGNORECASE):
            return sev
    return "WARNING"

def get_function_name(lines_before: list[str]) -> str:
    for line in reversed(lines_before):
        line_stripped = line.strip()
        m = re.match(r"async\s+def\s+(\w+)|def\s+(\w+)", line_stripped)
        if m:
            return m.group(1) or m.group(2)
        m = re.match(r"class\s+(\w+)", line_stripped)
        if m:
            return m.group(1)
    return "<module>"

def process_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    new_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        
        m = re.match(r"^(\s*)except\s+Exception\b", line)
        if m:
            indent = m.group(1)
            # Check if next meaningful line already has record
            j = i + 1
            has_record = False
            block_lines = []
            while j < len(lines):
                blk = lines[j]
                stripped = blk.strip()
                if stripped and not blk.startswith(indent + " ") and not blk.startswith(indent + "\t"):
                    if not blk.startswith(indent):
                        break
                block_lines.append(blk)
                if "get_aggregator().record" in blk:
                    has_record = True
                j += 1
            
            if not has_record:
                context_start = max(0, i - 5)
                context = "".join(lines[context_start:i+1])
                severity = get_severity(context)
                func_name = get_function_name(lines[:i])
                
                exc_var = None
                exc_m = re.search(r"except\s+Exception\s+as\s+(\w+)", line)
                if exc_m:
                    exc_var = exc_m.group(1)
                else:
                    line = line.rstrip("\n")
                    if line.endswith(":"):
                        line = line[:-1] + " as _exc:"
                    else:
                        line = line.replace(":", " as _exc:", 1)
                    line += "\n"
                    exc_var = "_exc"
                
                record_line = f'{indent}    get_aggregator().record(ErrorSeverity.{severity}, __name__, "{func_name}", {exc_var})\n'
                new_lines.append(line)
                new_lines.append(record_line)
                new_lines.extend(block_lines)
                i = j
                continue
            else:
                new_lines.append(line)
                new_lines.extend(block_lines)
                i = j
                continue
        
        new_lines.append(line)
        i += 1
    
    path.write_text("".join(new_lines), encoding="utf-8")
    print(f"Processed {path}")

if __name__ == "__main__":
    for p in [
        Path("xmclaw/daemon/agent_loop.py"),
        Path("xmclaw/daemon/hop_loop.py"),
        Path("xmclaw/daemon/factory.py"),
    ]:
        process_file(p)
