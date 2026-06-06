# XMclaw 技能系统「格式解析」诊断报告

> 诊断员：诊断员_格式兼容性  
> 日期：2026-06-06  
> 范围：`xmclaw/skills/user_loader.py`、`markdown_skill.py`、`manifest.py`、`base.py`

---

## 一、执行摘要

经过对核心解析路径的**实机代码审查 + 构造数据驱动测试**，共发现 **8 个缺陷**，其中 **2 个为阻断点**（会导致加载流程崩溃或抛出未捕获异常），其余 6 个为字段缺失、类型不匹配或解析逻辑缺陷，导致用户遵循标准格式编写的技能无法被正确识别或 manifest 信息丢失。

---

## 二、阻断点（Critical）

### 阻断点 1：`_instantiate` 抛出的 `RuntimeError` 未被 `_load_one` 捕获

**位置**：`xmclaw/skills/user_loader.py` 第 380 行附近  
**现象**：当 `skill.py` 中的 `build_skill()` 工厂返回非 `Skill` 子类时，整个 `load_all()` 崩溃，而非返回 `ok=False` 的 `LoadResult`。

**代码片段**（`user_loader.py:709-720`）：

```python
def _instantiate(self, module: object) -> Skill | None:
    factory = getattr(module, "build_skill", None)
    if callable(factory):
        try:
            inst = factory()
        except Exception as exc:
            raise RuntimeError(
                f"build_skill() raised {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(inst, Skill):
            raise RuntimeError(          # ← 第 718 行抛出
                f"build_skill() returned {type(inst).__name__}, "
                f"not a Skill subclass"
            )
        return inst
```

**调用方**（`user_loader.py:342-483`）中，`_load_one` 仅在 `importlib` 阶段有 `try/except`（第 365-378 行），但第 380 行调用 `_instantiate` 时**没有任何包裹**：

```python
# 第 380 行 —— 裸调用，RuntimeError 直接上抛
skill_instance = self._instantiate(module)
```

**实际执行结果**：

```
--- Case 4: build_skill() returns non-Skill ---
  UNCAUGHT RuntimeError escaped _load_one: build_skill() returned str, not a Skill subclass
  THIS IS A BUG: _load_one should catch factory errors, not leak them
```

**影响**：一个用户目录中存在格式错误的 `skill.py`（工厂返回字符串、数字等），会导致**整个启动加载流程中断**，其他合法技能也无法注册。

**修复建议**：在 `_load_one` 第 380 行前后添加 `try/except RuntimeError`，将异常转换为 `LoadResult(ok=False, error=...)`。

---

### 阻断点 2：`reload_one` 中 `instance.id` 直接访问导致 `AttributeError`

**位置**：`xmclaw/skills/user_loader.py` 第 311-313 行  
**现象**：当 `skill.py` 中的 Skill 子类**遗漏了 `id` 属性**时，`reload_one()` 抛出未捕获的 `AttributeError`，而非返回错误字符串。

**代码片段**：

```python
# 第 311 行：getattr 安全
if getattr(instance, "id", None) != skill_id:
    return None, None, (
        # 第 313 行：直接访问 instance.id —— 若属性不存在则崩溃
        f"reloaded Skill.id {instance.id!r} != dir {skill_id!r}"
    )
```

**实际执行结果**：

```
Testing reload_one with missing id...
  UNCAUGHT: AttributeError: 'NoIdSkill' object has no attribute 'id'
```

**影响**：`SkillsWatcher` 在检测到文件变更后调用 `reload_one`，若用户编辑时误删了 `id` 属性，**守护进程崩溃**。

**修复建议**：将 `instance.id!r` 改为 `getattr(instance, 'id', None)!r`。

---

## 三、字段缺失 / 类型不匹配（High）

### 缺陷 3：`_load_manifest` 完全忽略 `permissions_enforced` 字段

**位置**：`xmclaw/skills/user_loader.py` 第 784-806 行  
**现象**：`manifest.json` 中显式声明了 `"permissions_enforced": true`，但解析后始终为默认值 `False`。

**代码片段**（`_load_manifest` 的 `return SkillManifest(...)` 构造调用）：

```python
return SkillManifest(
    id=skill_id,
    version=ver,
    title=str(data.get("title", "") or ""),
    description=str(data.get("description", "") or ""),
    permissions_fs=_as_tuple("permissions_fs"),
    permissions_net=_as_tuple("permissions_net"),
    permissions_subprocess=_as_tuple("permissions_subprocess"),
    max_cpu_seconds=float(data.get("max_cpu_seconds", 30.0)),
    max_memory_mb=int(data.get("max_memory_mb", 512)),
    created_by=str(data.get("created_by", "user")),
    evidence=_as_tuple("evidence"),
    triggers=_as_tuple("triggers"),
    when_to_use=_first_str("when_to_use", "whenToUse"),
    allowed_tools=_first_tuple("allowed_tools", "allowedTools"),
    paths=_as_tuple("paths"),
    requires_restart=bool(...),
    model=str(data.get("model", "") or ""),
    # ← 缺失：permissions_enforced 和 trust_level 完全没有被读取
)
```

**实际执行结果**：

```
permissions_enforced: False
trust_level: <SkillTrustLevel.USER: 'user'>
Expected permissions_enforced=True, got False
Expected trust_level=builtin, got user
```

**影响**：
- 用户无法通过 `manifest.json` 声明权限已强制执行（`permissions_enforced`）。
- UI 上所有技能都显示 "advisory" 徽章，即使作者明确写了 `"permissions_enforced": true`。

**修复建议**：在 `_load_manifest` 中添加：

```python
permissions_enforced=bool(data.get("permissions_enforced", False)),
trust_level=SkillTrustLevel(data.get("trust_level", "user")) if data.get("trust_level") else SkillTrustLevel.USER,
```

---

### 缺陷 4：`SkillManifest.to_dict()` 中 `trust_level` 保持为 `SkillTrustLevel` 枚举对象

**位置**：`xmclaw/skills/manifest.py` 第 175-191 行  
**现象**：`to_dict()` 仅将 `tuple` 转换为 `list`，但未处理 `SkillTrustLevel` 枚举，导致输出字典中 `trust_level` 的值类型为 `SkillTrustLevel` 而非纯 `str`。

**代码片段**：

```python
def to_dict(self) -> dict[str, Any]:
    d = asdict(self)
    for k, v in list(d.items()):
        if isinstance(v, tuple):
            d[k] = list(v)
    return d
```

**实际执行结果**：

```
trust_level value: <SkillTrustLevel.USER: 'user'>
trust_level type: SkillTrustLevel
```

**影响**：虽然 `SkillTrustLevel(str, Enum)` 继承了 `str`，`json.dumps` 可以序列化，但：
- 某些严格类型的 JSON 编码器（如 `orjson`、前端 TypeScript 运行时）可能不识别。
- 调用方做 `d["trust_level"] == "user"` 时，Python 中 `str` 子类比较可以工作，但 `isinstance(d["trust_level"], str)` 为 `True`，`type(...) is str` 为 `False`，可能引发隐蔽的类型检查失败。

**修复建议**：在 `to_dict()` 中添加枚举到原始值的转换：

```python
if isinstance(v, Enum):
    d[k] = v.value
```

---

## 四、SKILL.md Frontmatter 解析缺陷（Medium）

### 缺陷 5：多行 YAML scalar（`>` / `|` 折叠块）不被支持，且产生垃圾解析结果

**位置**：`xmclaw/skills/user_loader.py` 第 854-934 行（`_parse_skill_md_frontmatter`）  
**现象**：文档明确说明 "Multi-line YAML scalars are not supported"，但当用户写了标准 YAML 多行 `description: >` 时，解析结果不是空字符串或回退，而是把 `>` 本身当作 description 值。

**实际执行结果**：

```
--- Bug 1: Multi-line YAML scalar (folded block) ---
title='Title', description='>'
NOTE: Parser doc says multi-line scalars are NOT supported
```

**输入**：

```yaml
---
description: >
  This is a long description
  that spans multiple lines.
---
```

**输出**：`description='>'`

**影响**：用户在 SKILL.md 中写多行描述（非常常见）时，UI 显示为 `>` 或空，体验极差。

**修复建议**：在逐行扫描 frontmatter 时，检测到 `>` 或 `|` 行后，进入块读取模式，收集缩进行直到下一个非缩进键或 `---` 结束。

---

### 缺陷 6：`_parse_skill_md_created_by` 正则过于严格，拒绝合法值

**位置**：`xmclaw/skills/user_loader.py` 第 1022-1036 行  
**现象**：正则表达式 `^created_by:\s*['"]?([a-zA-Z_][a-zA-Z_0-9]*)['"]?\s*$` 要求值必须是 Python 标识符格式。

**实际执行结果**：

```
Regex: '^created_by:\s*[\'"]?([a-zA-Z_][a-zA-Z_0-9]*)[\'"]?\s*$'
  'created_by: evolved' -> 'evolved'
  "created_by: 'evolved'" -> 'evolved'
  'created_by: "evolved"' -> 'evolved'
  'created_by: Evolved' -> 'Evolved'
  'created_by: 123' -> None               ← 拒绝
  'created_by: evolved-user' -> None       ← 拒绝（连字符）
  'created_by: evolved_user' -> 'evolved_user'
  'created_by: evolved user' -> None      ← 拒绝（空格）
```

**影响**：
- `created_by: 123`（虽然少见）被拒绝。
- `created_by: evolved-user` 或 `created_by: evolved user` 等常见写法被拒绝，导致 `created_by` 回退为 `"user"`，抹杀了迁移/进化标记。

**修复建议**：放宽正则，允许任意非空标量值，例如：

```python
_CREATED_BY_RE = re.compile(
    r"^created_by:\s*['\"]?([^'\"\n]+)['\"]?\s*$",
    re.MULTILINE,
)
```

---

### 缺陷 7：`Skill` 基类未声明 `id`/`version` 类属性，导致错误信息误导

**位置**：`xmclaw/skills/base.py` 第 21-23 行  
**现象**：`Skill` 是一个纯 `abc.ABC`，没有声明 `id` 和 `version`。子类必须自己添加这两个类属性，否则 `getattr(instance, "id", None)` 返回 `None`。

**代码片段**：

```python
class Skill(abc.ABC):
    id: str        # ← 只有类型注解，没有赋值，不是类属性
    version: int   # ← 同上

    @abc.abstractmethod
    async def run(self, inp: SkillInput) -> SkillOutput: ...
```

**实际执行结果**：

```
Skill.id exists on class? False
Skill.version exists on class? False
```

**影响**：
- 用户忘记写 `id = "..."` 时，`_load_one` 的错误信息是：
  > "directory name 'foo' disagrees with Skill.id **None**"
- 这会让用户以为是目录名问题，而不是自己忘了在类里写 `id`。

**修复建议**：在 `Skill` 基类中赋予显式的占位值（如 `id = ""`、`version = 0`），或让 `_load_one` 在 `instance_id is None` 时给出更精确的错误提示。

---

## 五、其他观察

### 观察 A：`reload_one` 与 `_load_one` 的 `_instantiate` 异常处理不一致

`_load_one` 对 `importlib` 失败有 `try/except`（第 365-378 行），但对 `_instantiate` 没有。`reload_one` 对 `importlib` 失败有 `try/except`（第 294-303 行），但对 `_instantiate` 同样没有。两者都应统一包裹 `_instantiate` 的调用。

### 观察 B：`manifest.json` 的 `trust_level` 在 `_load_manifest` 中丢失

虽然 `_load_one` 和 `reload_one` 在调用 `_load_manifest` 后会用 `_trust_for()` 覆盖 `trust_level`，但如果未来有其他代码路径直接调用 `_load_manifest`（如 CLI 的 `xmclaw skill inspect`），manifest 中的 `trust_level` 声明会被静默丢弃。

---

## 六、修复优先级建议

| 优先级 | 缺陷 | 文件 | 行号 | 修复工作量 |
|--------|------|------|------|-----------|
| P0 | `_instantiate` RuntimeError 未捕获 | `user_loader.py` | ~380 | 加 try/except |
| P0 | `reload_one` `instance.id` AttributeError | `user_loader.py` | 313 | 改 getattr |
| P1 | `_load_manifest` 忽略 `permissions_enforced` | `user_loader.py` | 784-806 | 加字段读取 |
| P1 | `to_dict()` 不转换 `SkillTrustLevel` | `manifest.py` | 187-191 | 加 Enum 处理 |
| P2 | 多行 YAML scalar 解析错误 | `user_loader.py` | 854-934 | 加块读取逻辑 |
| P2 | `created_by` 正则过严 | `user_loader.py` | 1022-1036 | 放宽正则 |
| P2 | `Skill` 基类无 `id`/`version` 占位 | `base.py` | 21-23 | 加默认值或改进错误信息 |

---

## 七、测试脚本

本次诊断使用的测试脚本保存在：

- `diagnosis/_test_format_compat.py` — 主测试脚本，覆盖 frontmatter、loader、manifest、to_dict、验证边界
- `diagnosis/_test_reload_bug.py` — 验证 `reload_one` AttributeError
- `diagnosis/_test_manifest_fields.py` — 验证 manifest 字段丢失

执行方式：

```bash
.venv/Scripts/python.exe diagnosis/_test_format_compat.py
.venv/Scripts/python.exe diagnosis/_test_reload_bug.py
.venv/Scripts/python.exe diagnosis/_test_manifest_fields.py
```

---

*报告结束。*
