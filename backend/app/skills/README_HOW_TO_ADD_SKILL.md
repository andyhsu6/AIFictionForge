# Skill 开发指南：如何添加或修改 Skill

## 一、Skill 系统架构概览

```
backend/app/skills/          ← 所有 Skill 存放目录
├── story-short-write/       ← 每个 Skill 一个目录
│   ├── SKILL.md            ← 必须存在：YAML元数据 + 工作流指令
│   └── references/         ← 可选：参考知识库（.md 文件）
│       ├── xxx.md
│       └── ...
├── story-long-write/
│   ├── SKILL.md
│   └── references/...
└── ...

backend/app/services/skill_loader.py   ← Skill 加载器（自动扫描 skills/ 目录）
backend/app/api/skills.py              ← API 端点（/api/skills/list）
backend/app/services/prompt_service.py ← 将 Skill 注入系统提示词
```

**核心机制**：系统启动时，`skill_loader.py` 自动扫描 `skills/` 目录，读取每个子目录的 `SKILL.md`，解析 YAML 元数据和工作流指令，拼接 `references/` 参考资料后提供给 AI。

---

## 二、添加新 Skill（3 步）

### 步骤 1：创建 Skill 目录

在 `backend/app/skills/` 下创建新目录，目录名建议用小写英文+短横线：

```bash
mkdir -p backend/app/skills/my-new-skill/references
```

### 步骤 2：创建 SKILL.md（必须）

在目录下创建 `SKILL.md`，格式固定为 **YAML frontmatter + Markdown 正文**：

```markdown
---
name: my-new-skill
description: |
  一句话描述这个 Skill 的功能。这是显示在 UI 上的名称。
  触发方式：/my-new-skill、「帮我做xxx」
---

# my-new-skill：Skill 显示标题

你是 xxx 专家。你的任务是帮用户完成 xxx。

## 核心原则

- 原则1...
- 原则2...

## 工作流程

### Phase 1：需求确认
...

### Phase 2：执行
...

### Phase 3：输出
...

## 输出格式
...
```

**YAML frontmatter 字段说明**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | Skill 内部标识名，与目录名保持一致 |
| `description` | ✅ | Skill 描述。**第一句**（第一个句号前）会作为 UI 下拉框显示名称 |

**自动分类逻辑**（在 `skill_loader.py` 中）：

| name 包含 | UI 分类显示 |
|-----------|-------------|
| `long` | Skill·长篇 |
| `short` | Skill·短篇 |
| `deslop` | Skill·润色 |
| `browser` | Skill·工具 |
| 其他 | Skill |

### 步骤 3：添加参考资料（可选）

在 `references/` 目录下放入 `.md` 文件，每个文件会自动作为「参考资料」拼接到 SKILL.md 内容后面：

```
references/
├── technique-a.md      ← 自动加载，标题为 "参考资料：technique-a"
├── technique-b.md      ← 自动加载，标题为 "参考资料：technique-b"
└── examples.md
```

**完成！** 重启服务后新 Skill 自动出现在 UI 下拉框中，无需改任何代码。

---

## 三、修改现有 Skill

### 修改工作流指令

直接编辑对应 Skill 目录下的 `SKILL.md` 文件中 `---` 下方的 Markdown 部分。

### 修改元数据（名称、描述）

编辑 `SKILL.md` 文件开头的 YAML frontmatter：

```yaml
---
name: story-short-write          ← 修改内部标识
description: |                   ← 修改描述（第一句是 UI 显示名）
  新的一句话描述。后续详细说明...
---
```

### 增删参考资料

在 `references/` 目录下增删 `.md` 文件即可，重启后自动生效。

### 修改自动分类

编辑 `backend/app/services/skill_loader.py` 中的分类逻辑（约第158-167行）：

```python
# 确定 Skill 子分类
sub_category = "Skill"
if "long" in name:
    sub_category = "Skill·长篇"
elif "short" in name:
    sub_category = "Skill·短篇"
# 👇 在这里添加新的分类规则
elif "my-keyword" in name:
    sub_category = "Skill·新分类"
```

---

## 四、Skill 在系统中的流转路径

了解 Skill 如何被使用，方便排查问题：

### 4.1 SkillChat 页面（独立对话）

```
用户在 SkillChat 选择 Skill
  → 前端调用 /api/skills/list 获取列表
  → 前端调用 /api/skills/execute 发送消息
  → 后端 skill_loader 加载 SKILL.md + references
  → 注入到 system prompt → 调用 AI → 返回结果
```

### 4.2 章节生成（单章 / 批量）

```
用户选择 Skill 下拉框
  → 前端将 skill_key 传给后端
  → 后端从 prompt_service 获取 Skill 内容
  → 注入到章节生成的 system prompt 中
  → AI 在 Skill 指导下生成章节内容
```

关键代码位置：
- **后端注入点**：`backend/app/services/prompt_service.py` 的 `get_skill_content()` 方法
- **单章生成**：`backend/app/store/hooks.ts` → `generateChapterContentStream()`
- **批量生成**：`backend/app/api/chapters.py` → `generate_single_chapter_for_batch()`

---

## 五、完整示例：添加一个「节奏检查」Skill

```bash
# 1. 创建目录
mkdir -p backend/app/skills/story-pacing-check/references

# 2. 创建 SKILL.md
cat > backend/app/skills/story-pacing-check/SKILL.md << 'EOF'
---
name: story-pacing-check
description: |
  节奏诊断。分析已有章节的叙事节奏，指出拖沓/仓促之处并给出修改建议。
  触发方式：/story-pacing-check、「检查节奏」「节奏有问题」
---

# story-pacing-check：叙事节奏诊断

你是一位叙事节奏分析专家。你的任务是帮用户分析章节的节奏问题。

## 分析维度

### 1. 信息密度
- 每段是否有新信息推进？
- 是否存在重复表达？

### 2. 情绪曲线
- 情绪是否有起伏？
- 高潮前是否有足够铺垫？

### 3. 场景切换
- 场景转换是否自然？
- 时间线是否清晰？

## 输出格式

给出：问题位置 → 原因分析 → 修改建议
EOF

# 3. 重启服务即可！
```

---

## 六、注意事项

1. **SKILL.md 编码**：必须是 UTF-8
2. **YAML 格式**：`description: |` 后的内容需要缩进
3. **命名规则**：目录名和 `name` 字段用小写+短横线（如 `my-skill-name`）
4. **缓存**：Skill 有内存缓存，修改后需重启服务。也可调用 `refresh_skills_cache()` 热刷新
5. **参考文件大小**：`references/` 中所有文件会拼接到 prompt 中，注意总大小不要超过模型上下文限制
6. **不需要改代码**：添加标准 Skill（只有 SKILL.md + references）无需修改任何 Python/TypeScript 代码