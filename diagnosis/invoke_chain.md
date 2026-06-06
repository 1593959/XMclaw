# XMclaw 技能系统调用链路诊断报告

> 诊断员：调用链路  
> 日期：2026-06-06  
> 结论：**发现 1 个核心阻断性 Bug + 1 个设计限制**

---

## 1. 读取的源代码

### 1.1 `xmclaw/skills/tool_bridge.py` — `invoke` 方法（第 257–378 行）

```python
    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()

        # meta-tool 短路
        if call.name == META_BROWSE_TOOL_NAME:
            return self._invoke_browse(call, t0)
        if call.name == META_INSTALL_TOOL_NAME:
            return await self._invoke_install(call, t0)
        ...
        if call.name == META_RUN_TOOL_NAME:
            args = dict(call.args or {})
            requested = args.get("skill_id")
            if not isinstance(requested, str) or not requested.strip():
                return self._error_result(...)
            skill_id = requested.strip()
            forwarded_args = args.get("args")
            if isinstance(forwarded_args, dict):
                call_args = forwarded_args
            elif forwarded_args is None:
                call_args = {}
            else:
                return self._error_result(...)
        else:
            skill_id = self._tool_name_to_skill_id(call.name)   # ← 关键行
            if skill_id is None:
                return self._error_result(
                    call.id, f"unknown skill tool: {call.name!r}", t0
                )
            call_args = dict(call.args or {})

        chosen_version = None
        if self._variant_selector is not None:
            try:
                chosen_version = self._variant_selector.pick_version(skill_id)
            except Exception:
                chosen_version = None
        try:
            skill = self._registry.get(skill_id, version=chosen_version)
        except UnknownSkillError as exc:
            return self._error_result(...)

        try:
            out = await skill.run(SkillInput(args=call_args))
        except Exception as exc:
            latency_ms = self._elapsed_ms(t0)
            self._registry.record_usage(skill_id, success=False, latency_ms=latency_ms)
            return self._error_result(...)

        latency_ms = self._elapsed_ms(t0)
        self._registry.record_usage(skill_id, success=bool(out.ok), latency_ms=latency_ms)

        effective_version = chosen_version
        if effective_version is None:
            try:
                effective_version = self._registry.active_version(skill_id)
            except Exception:
                effective_version = None
        result_metadata: dict[str, Any] = {}
        if effective_version is not None:
            result_metadata["skill_version"] = int(effective_version)
            result_metadata["skill_id"] = skill_id
        return ToolResult(
            call_id=call.id,
            ok=bool(out.ok),
            content=out.result,
            error=None if out.ok else _coerce_error(out.result),
            latency_ms=latency_ms,
            side_effects=tuple(out.side_effects or ()),
            metadata=result_metadata,
        )
```

### 1.2 `xmclaw/skills/tool_bridge.py` — `_spec_for` 方法（第 1604–1637 行）

```python
    def _spec_for(self, skill_id: str) -> ToolSpec | None:
        try:
            ref = self._registry.ref(skill_id)
        except UnknownSkillError:
            return None

        manifest = ref.manifest
        description = self._build_description(skill_id, manifest, ref.version)
        schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Arguments forwarded to Skill.run(SkillInput(args=...)). "
                "Pass whatever fields the skill's run() expects."
            ),
        }
        if manifest.paths:
            schema["x_paths"] = list(manifest.paths)
        if manifest.triggers:
            schema["x_triggers"] = list(manifest.triggers)
        return ToolSpec(
            name=_to_tool_name(skill_id),
            description=description,
            parameters_schema=schema,
        )
```

### 1.3 `xmclaw/skills/registry.py` — `get`、`register`、`promote`

```python
    def register(self, skill: Skill, manifest: SkillManifest, *, set_head: bool = True) -> SkillRef:
        skill_id = skill.id
        version = skill.version
        key = (skill_id, version)
        with self._lock:
            if key in self._skills:
                raise ValueError(f"{skill_id!r} v{version} already registered")
            if manifest.id != skill_id or manifest.version != version:
                raise ValueError("manifest id/version mismatch")
            self._skills[key] = skill
            self._manifests[key] = manifest
            versions = self._versions[skill_id]
            versions.append(version)
            versions.sort()
            if set_head and skill_id not in self._head:
                self._head[skill_id] = version
            return SkillRef(skill_id=skill_id, version=version, manifest=manifest)

    def get(self, skill_id: str, version: int | None = None) -> Skill:
        with self._lock:
            if version is None:
                if skill_id not in self._head:
                    raise UnknownSkillError(f"skill {skill_id!r} has no HEAD")
                version = self._head[skill_id]
            try:
                return self._skills[(skill_id, version)]
            except KeyError as exc:
                raise UnknownSkillError(f"skill {skill_id!r} v{version} not registered") from exc

    def promote(self, skill_id: str, to_version: int, *, evidence: list[str], source: str = "manual", force: bool = False) -> PromotionRecord:
        if not evidence:
            raise ValueError("anti-req #12: promotion refused without evidence")
        with self._lock:
            if (skill_id, to_version) not in self._skills:
                raise UnknownSkillError(f"cannot promote to unregistered version {skill_id!r} v{to_version}")
            ...
            self._head[skill_id] = to_version
            ...
```

### 1.4 `xmclaw/skills/base.py` — `Skill` 基类

```python
@dataclass(frozen=True, slots=True)
class SkillInput:
    args: dict[str, Any]

@dataclass(frozen=True, slots=True)
class SkillOutput:
    ok: bool
    result: Any
    side_effects: list[str]

class Skill(abc.ABC):
    id: str
    version: int

    @abc.abstractmethod
    async def run(self, inp: SkillInput) -> SkillOutput: ...
```

---

## 2. 端到端测试执行结果

### 2.1 测试环境

- 解释器：`.venv/Scripts/python.exe`
- 测试脚本：`diagnosis/test_invoke_chain.py`、`diagnosis/test_invoke_chain2.py`

### 2.2 测试 1：基础注册 → 直接调用 per-skill 工具

**结果：✅ 通过**

```
注册结果: SkillRef(skill_id='test.dummy', version=1, manifest=...)
list_skill_ids: ['test.dummy']
暴露的工具数: 10
  - skill_browse
  - skill_install
  - skill_uninstall
  - skill_status
  - skill_view
  - skill_run
  - skill_diff
  - skill_rollback
  - skill_propose
  - skill_test__dummy
invoke 结果: ok=True, content={'echo': {'foo': 'bar'}}
```

### 2.3 测试 2：`skill_run` meta-tool 调用路径

**结果：✅ 通过**

```
skill_run 结果: ok=True, content={'echo': {'key': 'value'}}
```

### 2.4 测试 3：`skill_browse` → `skill_view` 渐进披露

- `skill_browse`：**✅ 通过**，正确返回匹配列表
- `skill_view`：**⚠️ 设计限制**（非代码错误）

```
skill_view 结果: ok=False, error=skill dir not found for 'test.dummy' under any of [...]
```

`skill_view` 从磁盘 `~/.xmclaw/skills_user/` 或 `~/.agents/skills/` 读取文件，而不是从内存 registry 读取。对于纯内存测试 skill（无磁盘目录），这是预期失败。**这不是用户报告的核心问题**。

### 2.5 测试 4：`ToolCall` / `ToolSpec` / `ToolResult` 构造函数签名匹配

**结果：✅ 完全匹配，无错误**

```
ToolCall 构造成功: ToolCall(name='x', args={}, provenance='synthetic', id='...', ...)
ToolSpec 构造成功: ToolSpec(name='x', description='d', parameters_schema={...}, read_only=False)
ToolResult 构造成功: ToolResult(call_id='c1', ok=True, content='hello', ...)
```

### 2.6 测试 5：provider 创建后注册 skill

**结果：✅ 通过**（`list_tools` 每次都重新查询 registry，不依赖缓存）

```
provider 创建后注册的 skill 是否暴露: 1 个 per-skill 工具
invoke 结果: ok=True, error=None
```

### 2.7 测试 6：`_tool_name_cache` 缓存失效 — **核心阻断点**

**结果：❌ 失败**

```
provider2._tool_name_cache = {'skill_test__dummy': 'test.dummy'}
新 skill 调用: ok=False, error=unknown skill tool: 'skill_test__newskill'
调用后 provider2._tool_name_cache = {'skill_test__dummy': 'test.dummy'}
```

**复现路径**：
1. 创建 `SkillToolProvider`
2. 调用任意 per-skill 工具 → 触发 `_tool_name_cache` 构建
3. 注册新 skill（或 promote/rollback）
4. `list_tools()` 正确显示新工具
5. LLM 调用新工具 → `invoke` 返回 `unknown skill tool`

### 2.8 测试 A/B：promote / rollback 后缓存行为

**结果：promote/rollback 本身调用成功，但缓存不更新**

```
第一次调用: ok=True, content=v1: {}
缓存状态: {'skill_test__dummy': 'test.dummy'}
promote 后 list_skill_ids: ['test.dummy']
promote 后缓存状态: {'skill_test__dummy': 'test.dummy'}   ← 未更新
promote 后调用: ok=True, content=v2: {}                    ← 碰巧成功（skill_id 没变）

rollback 后缓存状态: {'skill_test__dummy': 'test.dummy'}   ← 仍未更新
rollback 后调用: ok=True, content=v1: {}                    ← 碰巧成功
```

**注意**：promote/rollback 不改变 skill_id，所以旧缓存的 key 仍然能命中。但如果 promote 的是一个**新 skill_id**（测试 C），或者 skill_id 被移除后 rollback，缓存就会失效。

### 2.9 测试 D：`skill_run` 是否受缓存影响

**结果：✅ 不受缓存影响**

```
skill_run 调用新 skill: ok=True, content=newskill
```

`skill_run` 直接从 `args.skill_id` 读取，不经过 `_tool_name_to_skill_id`，因此绕过缓存。这解释了为什么用户可能发现 "`skill_run` 有时能工作，但直接调用 `skill_xxx` 不行"。

---

## 3. 阻断点定位

### 🔴 阻断点 #1：`_tool_name_cache` 永不过期

| 项目 | 详情 |
|------|------|
| **文件** | `xmclaw/skills/tool_bridge.py` |
| **方法** | `_tool_name_to_skill_id`（第 1695–1706 行） |
| **问题代码** | ```python<br>def _tool_name_to_skill_id(self, tool_name: str) -> str \| None:<br>    if self._tool_name_cache is None:<br>        self._tool_name_cache = {<br>            _to_tool_name(sid): sid<br>            for sid in self._registry.list_skill_ids()<br>        }<br>    return self._tool_name_cache.get(tool_name)<br>``` |
| **根因** | 缓存字典在首次 `invoke` 时构建后，**没有任何失效机制**。daemon 运行期间的新 skill 注册、安装、promote、rollback 都不会刷新缓存。 |
| **症状** | `list_tools()` 能看到新工具，但 LLM 调用时返回 `unknown skill tool: 'skill_xxx'`。 |
| **影响范围** | 所有通过 per-skill 工具名（`skill_<id>`）的调用路径；`skill_run` 不受影响。 |

### 🟡 阻断点 #2（次要）：`skill_view` 只读磁盘，不读内存 registry

| 项目 | 详情 |
|------|------|
| **文件** | `xmclaw/skills/tool_bridge.py` |
| **方法** | `_invoke_view`（第 721–879 行） |
| **问题** | `skill_view` 通过 `resolve_skill_roots()` 在磁盘上查找 skill 目录，而不是从 `SkillRegistry` 读取已注册的内存对象。 |
| **症状** | 对于 programmatically 注册的 skill（无磁盘文件），`skill_view` 永远返回 `skill dir not found`。 |
| **优先级** | 低 — 这是设计选择，不是导致 "无法调用" 的元凶。 |

---

## 4. 修复建议

### 4.1 核心修复：使 `_tool_name_cache` 在 `list_tools()` 后失效

在 `list_tools()` 返回前清除缓存，确保 `list_tools` 与 `invoke` 看到的 registry 状态一致：

```python
# xmclaw/skills/tool_bridge.py 第 233 行附近
    def list_tools(self) -> list[ToolSpec]:
        specs = []
        specs.append(self._browse_spec())
        ...
        for skill_id in self._registry.list_skill_ids():
            spec = self._spec_for(skill_id)
            if spec is not None:
                specs.append(spec)
        self._tool_name_cache = None   # ← 添加此行
        return specs
```

**替代方案**（更保守）：在 `_tool_name_to_skill_id` 中，如果缓存 miss 则自动重建：

```python
    def _tool_name_to_skill_id(self, tool_name: str) -> str | None:
        if self._tool_name_cache is None:
            self._rebuild_cache()
        result = self._tool_name_cache.get(tool_name)
        if result is None:
            self._rebuild_cache()
            result = self._tool_name_cache.get(tool_name)
        return result

    def _rebuild_cache(self) -> None:
        self._tool_name_cache = {
            _to_tool_name(sid): sid
            for sid in self._registry.list_skill_ids()
        }
```

### 4.2 可选改进：`skill_view` 支持内存 skill

在 `_invoke_view` 中，如果磁盘查找失败，可回退到从 registry 读取 skill 的 manifest / description 信息，避免对纯内存 skill 返回硬错误。

---

## 5. 结论

XMclaw 技能系统 "无法正常调用技能" 的**唯一核心代码错误**是：

> **`SkillToolProvider._tool_name_cache` 在首次构建后永远不会更新。**

这导致 daemon 运行期间注册的新 skill 在 `list_tools()` 中可见，但 LLM 实际调用时（`invoke` → `_tool_name_to_skill_id`）因缓存 miss 而返回 `unknown skill tool`。`skill_run` 因绕过缓存而正常工作，形成了 "meta-tool 能跑，直接工具不能跑" 的混乱现象。

`ToolCall`、`ToolSpec`、`ToolResult` 的构造函数签名与调用代码完全匹配，无类型错误。`SkillRegistry` 的 `get`/`register`/`promote` 逻辑正确。`Skill` 基类定义清晰。
