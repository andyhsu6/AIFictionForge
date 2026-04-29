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
from typing import List, Dict, Optional
from app.logger import get_logger

logger = get_logger(__name__)

# Skills 目录路径：backend/app/skills/ （本文件在 backend/app/services/ 下）
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills")


def _parse_yaml_frontmatter(content: str) -> Dict[str, str]:
    """解析 SKILL.md 开头的 YAML frontmatter"""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if not match:
        return {}
    
    yaml_text = match.group(1)
    result = {}
    
    # 简单解析 YAML（不引入 pyyaml 依赖）
    name_match = re.search(r'^name:\s*(.+)$', yaml_text, re.MULTILINE)
    if name_match:
        result['name'] = name_match.group(1).strip()
    
    desc_match = re.search(r'description:\s*\|(.*?)^(?=\S)', yaml_text, re.MULTILINE | re.DOTALL)
    if desc_match:
        desc = desc_match.group(1).strip()
        # 清理缩进
        lines = desc.split('\n')
        min_indent = float('inf')
        for line in lines:
            if line.strip():
                indent = len(line) - len(line.lstrip())
                min_indent = min(min_indent, indent)
        if min_indent == float('inf'):
            min_indent = 0
        desc = '\n'.join(line[min_indent:] if line.strip() else '' for line in lines)
        result['description'] = desc.strip()
    else:
        desc_single = re.search(r'description:\s*"(.+?)"', yaml_text, re.DOTALL)
        if desc_single:
            result['description'] = desc_single.group(1).strip()
        else:
            desc_single2 = re.search(r'description:\s*(.+?)$', yaml_text, re.MULTILINE)
            if desc_single2:
                result['description'] = desc_single2.group(1).strip()
    
    return result


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
            
            # 构建触发词
            triggers = []
            name = metadata.get('name', skill_name)
            triggers.append(f"/{name}")
            
            # 从描述中提取中文触发词
            desc = metadata.get('description', '')
            trigger_match = re.findall(r'[「](.+?)[」]', desc)
            triggers.extend(trigger_match)
            
            # 拼接参考知识库到内容（作为按需加载的附录）
            if references:
                ref_section = "\n\n---\n\n## 附录：参考资料知识库\n"
                ref_section += "（以下内容根据用户需求按需引用，不需要全部使用）\n"
                for ref_name, ref_content in references.items():
                    ref_section += f"\n### 参考资料：{ref_name}\n\n{ref_content}\n"
                full_content = body + ref_section
            else:
                full_content = body
            
            # 确定 Skill 子分类
            sub_category = "Skill"
            if "long" in name:
                sub_category = "Skill·长篇"
            elif "short" in name:
                sub_category = "Skill·短篇"
            elif "deslop" in name:
                sub_category = "Skill·润色"
            elif "browser" in name:
                sub_category = "Skill·工具"
            
            skill_template = {
                "template_key": f"SKILL_{name.upper().replace('-', '_')}",
                "template_name": metadata.get('description', '').split('。')[0] if '。' in metadata.get('description', '') else name,
                "category": sub_category,
                "description": desc,
                "parameters": ["user_input"],
                "content": full_content,
                "references": references,
                "triggers": triggers,
                "is_skill": True,
            }
            
            skills.append(skill_template)
            logger.info(f"加载 Skill: {name} (分类: {sub_category}, 参考: {len(references)} 个)")
            
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