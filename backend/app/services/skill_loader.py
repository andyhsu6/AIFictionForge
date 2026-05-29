"""Skill 提示词加载器

从 backend/app/skills/ 目录动态加载 oh-story-claudecode 格式的 Skill，
将其转换为 PromptService 兼容的系统默认模板。

每个 Skill 目录结构：
  skills/{skill_name}/
  ├── SKILL.md          # YAML元数据 + 完整工作流指令
  └── references/       # 参考知识库（可选）
      ├── xxx.md
      └── ...
"""

import os
import re
from typing import Any, List, Dict, Optional
import yaml
from app.logger import get_logger

logger = get_logger(__name__)

# Skills 目录路径：backend/app/skills/ （本文件在 backend/app/services/ 下）
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills")


class _SkillYamlDumper(yaml.SafeDumper):
    pass


class _LiteralString(str):
    pass


def _literal_string_representer(dumper: yaml.SafeDumper, data: _LiteralString):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')


_SkillYamlDumper.add_representer(_LiteralString, _literal_string_representer)


def _parse_yaml_frontmatter(content: str) -> Dict[str, Any]:
    """解析 SKILL.md 开头的 YAML frontmatter"""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if not match:
        return {}

    yaml_text = match.group(1)

    try:
        metadata = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        logger.warning(f"解析 Skill YAML frontmatter 失败: {e}")
        return {}

    if not isinstance(metadata, dict):
        return {}

    result: Dict[str, Any] = {}
    for key in ("name", "display_name", "category", "description", "triggers"):
        value = metadata.get(key)
        if key == "triggers" and isinstance(value, list):
            result[key] = [str(item).strip() for item in value if str(item).strip()]
        elif value is not None:
            result[key] = str(value).strip()

    return result


def _template_key(name: str) -> str:
    return f"SKILL_{name.upper().replace('-', '_')}"


def _display_name_from_description(description: str, fallback: str) -> str:
    first_line = description.strip().splitlines()[0].strip() if description.strip() else ""
    if "。" in first_line:
        return first_line.split("。")[0].strip() or fallback
    return first_line or fallback


def _infer_category(name: str) -> str:
    if "long" in name:
        return "Skill·长篇"
    if "short" in name:
        return "Skill·短篇"
    if "deslop" in name:
        return "Skill·润色"
    if "browser" in name:
        return "Skill·工具"
    return "Skill"


def _extract_triggers(name: str, description: str, explicit_triggers: Any = None) -> List[str]:
    """获取触发词，优先使用结构化 triggers，旧文件则从描述中兼容提取。"""
    triggers: List[str] = []

    if isinstance(explicit_triggers, list):
        triggers.extend(str(item).strip() for item in explicit_triggers if str(item).strip())
    elif isinstance(explicit_triggers, str) and explicit_triggers.strip():
        triggers.extend(item.strip() for item in re.split(r'[\n,，、]+', explicit_triggers) if item.strip())

    if not triggers:
        triggers.append(f"/{name}")
        triggers.extend(re.findall(r'「(.+?)」', description))
        triggers.extend(match.group(1) for match in re.finditer(r'(?:^|[\s、，,。；;：:])(/[^\s、，,。；;：:「」]+)', description))

    if f"/{name}" not in triggers:
        triggers.insert(0, f"/{name}")

    return list(dict.fromkeys(triggers))


def _format_skill_frontmatter(metadata: Dict[str, Any]) -> str:
    data = {
        "name": metadata.get("name", ""),
        "display_name": metadata.get("display_name", ""),
        "category": metadata.get("category", "Skill"),
        "description": _LiteralString(metadata.get("description", "")),
        "triggers": metadata.get("triggers", []),
    }
    yaml_text = yaml.dump(
        data,
        Dumper=_SkillYamlDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{yaml_text}\n---"


def _get_skill_body(content: str) -> str:
    """获取 SKILL.md 中 YAML frontmatter 之后的内容（即工作流指令）"""
    match = re.match(r'^---\s*\n.*?\n---\s*\n', content, re.DOTALL)
    if match:
        return content[match.end():].strip()
    return content.strip()


def _get_references(skill_dir: str) -> Dict[str, str]:
    """读取 skill 目录下 references/ 中的所有 .md 文件"""
    refs_dir = os.path.join(skill_dir, "references")
    references = {}
    
    if not os.path.isdir(refs_dir):
        return references
    
    for filename in sorted(os.listdir(refs_dir)):
        if filename.endswith('.md'):
            filepath = os.path.join(refs_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    ref_name = filename[:-3]  # 去掉 .md 后缀
                    references[ref_name] = f.read().strip()
            except Exception as e:
                logger.warning(f"读取参考文件失败: {filepath}, 错误: {e}")
    
    return references


def load_skills() -> List[Dict]:
    """
    从 skills 目录加载所有 Skill，返回模板列表。
    格式与 PromptService.get_all_system_templates() 返回的一致。
    
    Returns:
        List[Dict]: Skill 模板列表，每个包含：
            - template_key: 模板键名 (SKILL_{name})
            - template_name: 显示名称
            - category: 分类 ("Skill")
            - description: 描述
            - parameters: 参数列表
            - content: 完整工作流指令内容
            - references: 参考知识库字典
            - triggers: 触发词列表
    """
    skills = []
    
    if not os.path.isdir(SKILLS_DIR):
        logger.warning(f"Skills 目录不存在: {SKILLS_DIR}")
        return skills
    
    for skill_name in sorted(os.listdir(SKILLS_DIR)):
        skill_dir = os.path.join(SKILLS_DIR, skill_name)
        if not os.path.isdir(skill_dir):
            continue
        
        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md_path):
            continue
        
        try:
            with open(skill_md_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 解析 YAML frontmatter
            metadata = _parse_yaml_frontmatter(content)
            
            # 获取工作流指令（去掉 YAML 部分）
            body = _get_skill_body(content)
            
            # 读取参考知识库
            references = _get_references(skill_dir)
            
            name = metadata.get('name', skill_name)
            desc = metadata.get('description', '')
            display_name = metadata.get('display_name') or _display_name_from_description(desc, name)
            category = metadata.get('category') or _infer_category(name)
            triggers = _extract_triggers(name, desc, metadata.get('triggers'))
            
            # 拼接参考知识库到内容（作为按需加载的附录）
            if references:
                ref_section = "\n\n---\n\n## 附录：参考资料知识库\n"
                ref_section += "（以下内容根据用户需求按需引用，不需要全部使用）\n"
                for ref_name, ref_content in references.items():
                    ref_section += f"\n### 参考资料：{ref_name}\n\n{ref_content}\n"
                full_content = body + ref_section
            else:
                full_content = body
            
            skill_template = {
                "template_key": _template_key(name),
                "name": name,
                "template_name": display_name,
                "display_name": display_name,
                "category": category,
                "description": desc,
                "parameters": ["user_input"],
                "content": full_content,
                "references": references,
                "triggers": triggers,
                "is_skill": True,
            }
            
            skills.append(skill_template)
            logger.info(f"加载 Skill: {name} (分类: {category}, 参考: {len(references)} 个)")
            
        except Exception as e:
            logger.error(f"加载 Skill 失败: {skill_name}, 错误: {e}")
    
    return skills


def get_skill_by_trigger(user_input: str) -> Optional[Dict]:
    """
    根据用户输入匹配对应的 Skill
    
    Args:
        user_input: 用户输入的文本
        
    Returns:
        匹配到的 Skill 模板，未匹配返回 None
    """
    skills = load_skills()
    user_input_lower = user_input.lower().strip()
    
    for skill in skills:
        triggers = skill.get('triggers', [])
        for trigger in triggers:
            trigger_lower = trigger.lower()
            # 精确匹配触发词
            if user_input_lower == trigger_lower:
                return skill
            # 用户输入以触发词开头
            if user_input_lower.startswith(trigger_lower):
                return skill
    
    # 自然语言模糊匹配
    keyword_map = {
        "长篇写作": ["SKILL_STORY_LONG_WRITE"],
        "写长篇": ["SKILL_STORY_LONG_WRITE"],
        "帮我开书": ["SKILL_STORY_LONG_WRITE"],
        "写大纲": ["SKILL_STORY_LONG_WRITE"],
        "短篇写作": ["SKILL_STORY_SHORT_WRITE"],
        "写短篇": ["SKILL_STORY_SHORT_WRITE"],
        "写个盐言": ["SKILL_STORY_SHORT_WRITE"],
        "长篇拆文": ["SKILL_STORY_LONG_ANALYZE"],
        "拆书": ["SKILL_STORY_LONG_ANALYZE"],
        "分析黄金三章": ["SKILL_STORY_LONG_ANALYZE"],
        "短篇拆文": ["SKILL_STORY_SHORT_ANALYZE"],
        "分析短篇": ["SKILL_STORY_SHORT_ANALYZE"],
        "长篇扫榜": ["SKILL_STORY_LONG_SCAN"],
        "长篇什么火": ["SKILL_STORY_LONG_SCAN"],
        "起点排行": ["SKILL_STORY_LONG_SCAN"],
        "短篇扫榜": ["SKILL_STORY_SHORT_SCAN"],
        "短篇什么火": ["SKILL_STORY_SHORT_SCAN"],
        "去ai味": ["SKILL_STORY_DESLOP"],
        "去味": ["SKILL_STORY_DESLOP"],
        "太ai了": ["SKILL_STORY_DESLOP"],
        "润色": ["SKILL_STORY_DESLOP"],
        "浏览器": ["SKILL_BROWSER_CDP"],
    }
    
    for keyword, skill_keys in keyword_map.items():
        if keyword in user_input_lower:
            for skill in skills:
                if skill['template_key'] in skill_keys:
                    return skill
    
    return None


# 预加载缓存
_skills_cache = None

def get_all_skills_cached() -> List[Dict]:
    """获取所有 Skills（带缓存）"""
    global _skills_cache
    if _skills_cache is None:
        _skills_cache = load_skills()
    return _skills_cache

def refresh_skills_cache():
    """刷新 Skills 缓存"""
    global _skills_cache
    _skills_cache = load_skills()
    return _skills_cache


def get_skill_detail(skill_key: str) -> Optional[Dict]:
    """根据 template_key 获取 Skill 完整详情（包括原始 SKILL.md 内容和独立 references）"""
    skills = get_all_skills_cached()
    for s in skills:
        if s["template_key"] == skill_key:
            # 找到对应的目录
            skill_name = skill_key.replace("SKILL_", "").lower().replace("_", "-")
            skill_dir = os.path.join(SKILLS_DIR, skill_name)
            if not os.path.isdir(skill_dir):
                # 尝试从 name 字段获取
                for d in os.listdir(SKILLS_DIR):
                    d_path = os.path.join(SKILLS_DIR, d)
                    if os.path.isdir(d_path):
                        md_path = os.path.join(d_path, "SKILL.md")
                        if os.path.isfile(md_path):
                            try:
                                with open(md_path, 'r', encoding='utf-8') as f:
                                    meta = _parse_yaml_frontmatter(f.read())
                                if f"SKILL_{meta.get('name', '').upper().replace('-', '_')}" == skill_key:
                                    skill_dir = d_path
                                    break
                            except:
                                pass

            # 读取原始 SKILL.md
            skill_md_path = os.path.join(skill_dir, "SKILL.md")
            raw_content = ""
            if os.path.isfile(skill_md_path):
                with open(skill_md_path, 'r', encoding='utf-8') as f:
                    raw_content = f.read()

            # 读取独立的 references（不拼接到 content 中）
            standalone_refs = {}
            refs_dir = os.path.join(skill_dir, "references")
            if os.path.isdir(refs_dir):
                for filename in sorted(os.listdir(refs_dir)):
                    if filename.endswith('.md'):
                        filepath = os.path.join(refs_dir, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                standalone_refs[filename[:-3]] = f.read()
                        except:
                            pass

            return {
                **s,
                "raw_content": raw_content,
                "standalone_references": standalone_refs,
                "skill_dir": skill_dir,
            }
    return None


def _validate_skill_metadata(name: str, display_name: str, category: str, description: str, triggers: List[str], body: str):
    if not name.strip():
        raise ValueError("Skill 内部标识不能为空")
    if not re.fullmatch(r'[a-z0-9][a-z0-9\-]*', name.strip()):
        raise ValueError("Skill 内部标识只能包含小写字母、数字和短横线，且必须以字母或数字开头")
    if not display_name.strip():
        raise ValueError("显示名称不能为空")
    if not category.strip():
        raise ValueError("分类不能为空")
    if not description.strip():
        raise ValueError("描述不能为空")
    if not body.strip():
        raise ValueError("工作流指令不能为空")
    if not triggers:
        raise ValueError("至少需要一个触发词")


def create_skill_files(
    name: str,
    description: str,
    body: str,
    references: Optional[Dict[str, str]] = None,
    display_name: Optional[str] = None,
    category: Optional[str] = None,
    triggers: Optional[List[str]] = None,
) -> Dict:
    """创建新的 Skill 文件"""
    import re
    name = name.strip().lower().replace("_", "-").replace(" ", "-")
    display_name = (display_name or _display_name_from_description(description, name)).strip()
    category = (category or _infer_category(name)).strip()
    triggers = _extract_triggers(name, description, triggers)
    _validate_skill_metadata(name, display_name, category, description, triggers, body)

    # 目录名：小写+短横线
    dir_name = name
    dir_name = re.sub(r'[^a-z0-9\-]', '', dir_name)
    if not dir_name:
        dir_name = "new-skill"
    
    skill_dir = os.path.join(SKILLS_DIR, dir_name)
    if os.path.exists(skill_dir):
        raise ValueError(f"Skill 目录已存在: {dir_name}")
    
    os.makedirs(skill_dir, exist_ok=True)
    
    frontmatter = _format_skill_frontmatter({
        "name": name,
        "display_name": display_name,
        "category": category,
        "description": description.strip(),
        "triggers": triggers,
    })
    skill_md_content = f"{frontmatter}\n\n{body.strip()}"
    
    skill_md_path = os.path.join(skill_dir, "SKILL.md")
    with open(skill_md_path, 'w', encoding='utf-8') as f:
        f.write(skill_md_content)
    
    # 创建 references
    if references:
        refs_dir = os.path.join(skill_dir, "references")
        os.makedirs(refs_dir, exist_ok=True)
        for ref_name, ref_content in references.items():
            ref_path = os.path.join(refs_dir, f"{ref_name}.md")
            with open(ref_path, 'w', encoding='utf-8') as f:
                f.write(ref_content)
    
    # 刷新缓存
    refresh_skills_cache()
    
    # 返回新建的 skill
    skills = get_all_skills_cached()
    for s in skills:
        if s["template_key"] == _template_key(name):
            return s
    return {"template_key": _template_key(name), "template_name": display_name, "category": category}


def update_skill_files(
    skill_key: str,
    description: Optional[str] = None,
    body: Optional[str] = None,
    references: Optional[Dict[str, str]] = None,
    display_name: Optional[str] = None,
    category: Optional[str] = None,
    triggers: Optional[List[str]] = None,
) -> Dict:
    """更新已有 Skill 文件"""
    detail = get_skill_detail(skill_key)
    if not detail:
        raise ValueError(f"未找到 Skill: {skill_key}")
    
    skill_dir = detail.get("skill_dir", "")
    if not skill_dir or not os.path.isdir(skill_dir):
        raise ValueError(f"Skill 目录不存在: {skill_dir}")
    
    skill_md_path = os.path.join(skill_dir, "SKILL.md")
    
    # 读取现有内容
    with open(skill_md_path, 'r', encoding='utf-8') as f:
        raw = f.read()
    
    # 解析现有元数据
    metadata = _parse_yaml_frontmatter(raw)
    name = metadata.get('name', '')
    
    # 更新 SKILL.md
    final_desc = description if description is not None else metadata.get('description', '')
    final_body = body if body is not None else _get_skill_body(raw)
    final_display_name = display_name if display_name is not None else metadata.get('display_name') or _display_name_from_description(final_desc, name)
    final_category = category if category is not None else metadata.get('category') or _infer_category(name)
    final_triggers = _extract_triggers(name, final_desc, triggers if triggers is not None else metadata.get('triggers'))
    _validate_skill_metadata(name, final_display_name, final_category, final_desc, final_triggers, final_body)

    frontmatter = _format_skill_frontmatter({
        "name": name,
        "display_name": final_display_name.strip(),
        "category": final_category.strip(),
        "description": final_desc.strip(),
        "triggers": final_triggers,
    })
    new_content = f"{frontmatter}\n\n{final_body.strip()}"
    
    with open(skill_md_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    # 更新 references
    if references is not None:
        refs_dir = os.path.join(skill_dir, "references")
        # 删除旧的 reference 文件
        if os.path.isdir(refs_dir):
            for f in os.listdir(refs_dir):
                if f.endswith('.md'):
                    os.remove(os.path.join(refs_dir, f))
        else:
            os.makedirs(refs_dir, exist_ok=True)
        
        # 写入新的 references
        for ref_name, ref_content in references.items():
            if ref_content.strip():  # 只写入非空内容
                ref_path = os.path.join(refs_dir, f"{ref_name}.md")
                with open(ref_path, 'w', encoding='utf-8') as f:
                    f.write(ref_content)
    
    # 刷新缓存
    refresh_skills_cache()
    
    # 返回更新后的详情
    return get_skill_detail(skill_key) or {}


def delete_skill_files(skill_key: str) -> bool:
    """删除 Skill 目录"""
    import shutil
    detail = get_skill_detail(skill_key)
    if not detail:
        raise ValueError(f"未找到 Skill: {skill_key}")
    
    skill_dir = detail.get("skill_dir", "")
    if not skill_dir or not os.path.isdir(skill_dir):
        raise ValueError(f"Skill 目录不存在")
    
    shutil.rmtree(skill_dir)
    refresh_skills_cache()
    return True
