import { useState, useEffect, useMemo } from 'react';
import { Button, List, Modal, Form, Input, message, Empty, Space, Popconfirm, Card, Select, Radio, Tag, InputNumber, Tabs, Pagination, theme, Upload, Alert, Divider } from 'antd';
import { EditOutlined, DeleteOutlined, ThunderboltOutlined, BranchesOutlined, AppstoreAddOutlined, CheckCircleOutlined, ExclamationCircleOutlined, PlusOutlined, FileTextOutlined, UploadOutlined, DownloadOutlined } from '@ant-design/icons';
import { useStore } from '../store';
import { eventBus, EventNames } from '../store/eventBus';
import { getProjectTasks, type TaskStatus } from '../services/backgroundTaskService';
import { useOutlineSync } from '../store/hooks';
import { generateOutlineBackground } from '../services/backgroundTaskService';
import { outlineApi, chapterApi, projectApi, characterApi } from '../services/api';
import type { ApiError, Character, OutlineImportMode, OutlineImportPreview } from '../types';
import { Trans, useTranslation } from 'react-i18next';

// 大纲生成请求数据类型
interface OutlineGenerateRequestData {
  project_id: string;
  genre: string;
  theme: string;
  chapter_count: number;
  narrative_perspective: string;
  target_words: number;
  requirements?: string;
  mode: 'auto' | 'new' | 'continue';
  story_direction?: string;
  plot_stage: 'development' | 'climax' | 'ending';
  model?: string;
  provider?: string;
  content_language?: 'auto' | 'zh' | 'en';
}

// 角色/组织条目类型（新格式）
interface CharacterEntry {
  name: string;
  type: 'character' | 'organization';
}

/**
 * 解析 characters 字段，兼容新旧格式
 * 旧格式: string[] -> 全部当作 character
 * 新格式: {name: string, type: "character"|"organization"}[]
 */
function parseCharacterEntries(characters: unknown): CharacterEntry[] {
  if (!Array.isArray(characters) || characters.length === 0) return [];
  
  return characters.map((entry) => {
    if (typeof entry === 'string') {
      // 旧格式：纯字符串，默认为 character
      return { name: entry, type: 'character' as const };
    }
    if (typeof entry === 'object' && entry !== null && 'name' in entry) {
      // 新格式：带类型标识的对象
      return {
        name: (entry as { name: string }).name,
        type: ((entry as { type?: string }).type === 'organization' ? 'organization' : 'character') as 'character' | 'organization'
      };
    }
    return null;
  }).filter((e): e is CharacterEntry => e !== null);
}

/** 从 entries 中提取角色名称列表 */
function getCharacterNames(entries: CharacterEntry[]): string[] {
  return entries.filter(e => e.type === 'character').map(e => e.name);
}

/** 从 entries 中提取组织名称列表 */
function getOrganizationNames(entries: CharacterEntry[]): string[] {
  return entries.filter(e => e.type === 'organization').map(e => e.name);
}

interface OutlineStructureData {
  key_events?: string[];
  key_points?: string[];
  characters_involved?: string[];
  characters?: unknown[];
  scenes?: string[] | Array<{
    location: string;
    characters: string[];
    purpose: string;
  }>;
  emotion?: string;
  goal?: string;
  title?: string;
  summary?: string;
  content?: string;
}

function parseOutlineStructure(structure?: string): OutlineStructureData {
  if (!structure) return {};
  try {
    return JSON.parse(structure) as OutlineStructureData;
  } catch (e) {
    console.error('parse structure failed:', e);
    return {};
  }
}

function getOutlinePreview(content: string, maxLength = 120): { text: string; truncated: boolean } {
  const normalized = (content || '').replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLength) {
    return { text: normalized, truncated: false };
  }
  return {
    text: `${normalized.slice(0, maxLength).trimEnd()}...`,
    truncated: true
  };
}

const { TextArea } = Input;

export default function Outline() {
  const { t } = useTranslation('outline');
  const { currentProject, outlines, setCurrentProject } = useStore();
  const [isGenerating, setIsGenerating] = useState(false);
  const [editForm] = Form.useForm();
  const [generateForm] = Form.useForm();
  const [expansionForm] = Form.useForm();
  const [modalApi, contextHolder] = Modal.useModal();
  const [batchExpansionForm] = Form.useForm();
  const [manualCreateForm] = Form.useForm();
  const [isMobile, setIsMobile] = useState(window.innerWidth <= 768);
  const [isExpanding, setIsExpanding] = useState(false);
  const [projectCharacters, setProjectCharacters] = useState<Array<{ label: string; value: string }>>([]);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importMode, setImportMode] = useState<OutlineImportMode>('append');
  const [importPreview, setImportPreview] = useState<OutlineImportPreview | null>(null);
  const [isPreviewingImport, setIsPreviewingImport] = useState(false);
  const [isImporting, setIsImporting] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) =>
    `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;

  // ✅ 新增：记录大纲卡片内容的展开/折叠状态（默认折叠）
  const [outlineContentExpandStatus, setOutlineContentExpandStatus] = useState<Record<string, boolean>>({});

  // ✅ 新增：记录场景区域的展开/折叠状态
  const [scenesExpandStatus, setScenesExpandStatus] = useState<Record<string, boolean>>({});

  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth <= 768);
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // 大纲查询与分页状态
  const [outlineSearchKeyword, setOutlineSearchKeyword] = useState('');
  const [outlinePage, setOutlinePage] = useState(1);
  const [outlinePageSize, setOutlinePageSize] = useState(20);

  // 使用同步 hooks
  const {
    refreshOutlines,
    updateOutline,
    deleteOutline
  } = useOutlineSync();

  // 初始加载大纲列表和角色列表
  useEffect(() => {
    if (currentProject?.id) {
      refreshOutlines();
      // 加载项目角色列表
      loadProjectCharacters();
      // 检查是否有活跃的大纲生成任务，恢复按钮禁用状态
      checkActiveOutlineTasks();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProject?.id]); // 只依赖 ID，不依赖函数

  // 检查是否有活跃的大纲生成任务（页面切换后恢复状态）
  const checkActiveOutlineTasks = async () => {
    if (!currentProject?.id) return;
    try {
      const result = await getProjectTasks(currentProject.id, 'outline_new', 5);
      const result2 = await getProjectTasks(currentProject.id, 'outline_continue', 5);
      const allTasks = [...(result.items || []), ...(result2.items || [])];
      const hasActive = allTasks.some((t: TaskStatus) => t.status === 'running' || t.status === 'pending');
      setIsGenerating(hasActive);
    } catch (error) {
      console.error('check active outline tasks failed:', error);
    }
  };

  // 加载项目角色列表
  const loadProjectCharacters = async () => {
    if (!currentProject?.id) return;
    try {
      const characters = await characterApi.getCharacters(currentProject.id);
      setProjectCharacters(
        characters.map((char: Character) => ({
          label: char.name,
          value: char.name
        }))
      );
    } catch (error) {
      console.error('load characters failed:', error);
    }
  };

  // 从后端返回字段直接构建展开状态，避免前端 N+1 请求
  const outlineExpandStatus = useMemo(() => {
    const statusMap: Record<string, boolean> = {};
    outlines.forEach((outline) => {
      statusMap[outline.id] = Boolean(outline.has_chapters);
    });
    return statusMap;
  }, [outlines]);

  // 统一预解析 structure，避免 render 阶段重复 JSON.parse
  const outlineStructureMap = useMemo(() => {
    const parsedMap: Record<string, OutlineStructureData> = {};
    outlines.forEach((outline) => {
      parsedMap[outline.id] = parseOutlineStructure(outline.structure);
    });
    return parsedMap;
  }, [outlines]);

  // 当角色确认数据变化时，初始化选中状态（默认全选）
  // 当组织确认数据变化时，初始化选中状态（默认全选）
  // 移除事件监听，避免无限循环
  // Hook 内部已经更新了 store，不需要再次刷新

  // 确保大纲按 order_index 排序
  const sortedOutlines = [...outlines].sort((a, b) => a.order_index - b.order_index);

  // 前端查询过滤
  const filteredOutlines = useMemo(() => {
    const keyword = outlineSearchKeyword.trim().toLowerCase();
    if (!keyword) return sortedOutlines;

    return sortedOutlines.filter((outline) => {
      return (
        String(outline.order_index).includes(keyword) ||
        outline.title.toLowerCase().includes(keyword) ||
        outline.content.toLowerCase().includes(keyword)
      );
    });
  }, [sortedOutlines, outlineSearchKeyword]);

  // 当前分页数据
  const pagedOutlines = useMemo(() => {
    const start = (outlinePage - 1) * outlinePageSize;
    return filteredOutlines.slice(start, start + outlinePageSize);
  }, [filteredOutlines, outlinePage, outlinePageSize]);

  // 搜索词或页大小变化时，回到第一页
  useEffect(() => {
    setOutlinePage(1);
  }, [outlineSearchKeyword, outlinePageSize]);

  // 数据变化导致页码越界时自动纠正
  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filteredOutlines.length / outlinePageSize));
    if (outlinePage > maxPage) {
      setOutlinePage(maxPage);
    }
  }, [filteredOutlines.length, outlinePage, outlinePageSize]);

  if (!currentProject) return null;

  const handleOpenEditModal = (id: string) => {
    const outline = outlines.find(o => o.id === id);
    if (outline) {
      const structureData = outlineStructureMap[outline.id] || {};
      
      // 解析角色/组织条目（兼容新旧格式）
      const editEntries = parseCharacterEntries(structureData.characters);
      const editCharNames = getCharacterNames(editEntries);
      const editOrgNames = getOrganizationNames(editEntries);
      
      // 处理场景数据 - 可能是字符串数组或对象数组
      let scenesText = '';
      if (structureData.scenes) {
        if (typeof structureData.scenes[0] === 'string') {
          // 字符串数组格式
          scenesText = (structureData.scenes as string[]).join('\n');
        } else {
          // 对象数组格式
          scenesText = (structureData.scenes as Array<{location: string; characters: string[]; purpose: string}>)
            .map(s => `${s.location}|${(s.characters || []).join('、')}|${s.purpose}`)
            .join('\n');
        }
      }
      
      // 处理情节要点数据
      const keyPointsText = structureData.key_points ? structureData.key_points.join('\n') : '';
      
      // 设置表单初始值
      editForm.setFieldsValue({
        title: outline.title,
        content: outline.content,
        characters: editCharNames,
        organizations: editOrgNames,
        scenes: scenesText,
        key_points: keyPointsText,
        emotion: structureData.emotion || '',
        goal: structureData.goal || ''
      });
      
      modalApi.confirm({
        title: t('editModal.title'),
        width: 800,
        centered: true,
        styles: {
          body: {
            maxHeight: 'calc(100vh - 200px)',
            overflowY: 'auto'
          }
        },
        content: (
          <Form
            form={editForm}
            layout="vertical"
            style={{ marginTop: 12 }}
          >
            <Form.Item
              label={t('editModal.titleLabel')}
              name="title"
              rules={[{ required: true, message: t('editModal.titleRequired') }]}
              style={{ marginBottom: 12 }}
            >
              <Input placeholder={t('editModal.titlePh')} />
            </Form.Item>

            <Form.Item
              label={t('editModal.contentLabel')}
              name="content"
              rules={[{ required: true, message: t('editModal.contentRequired') }]}
              style={{ marginBottom: 12 }}
            >
              <TextArea rows={4} placeholder={t('editModal.contentPh')} />
            </Form.Item>
            
            <Form.Item
              label={t('editModal.charLabel')}
              name="characters"
              tooltip={t('editModal.charTooltip')}
              style={{ marginBottom: 12 }}
            >
              <Select
                mode="tags"
                style={{ width: '100%' }}
                placeholder={t('editModal.charPh')}
                options={projectCharacters}
                tokenSeparators={[',', '，']}
                maxTagCount="responsive"
              />
            </Form.Item>
            
            <Form.Item
              label={t('editModal.orgLabel')}
              name="organizations"
              tooltip={t('editModal.orgTooltip')}
              style={{ marginBottom: 12 }}
            >
              <Select
                mode="tags"
                style={{ width: '100%' }}
                placeholder={t('editModal.orgPh')}
                tokenSeparators={[',', '，']}
                maxTagCount="responsive"
              />
            </Form.Item>
            
            <Form.Item
              label={t('editModal.scenesLabel')}
              name="scenes"
              tooltip={t('editModal.scenesTooltip')}
              style={{ marginBottom: 12 }}
            >
              <TextArea
                rows={3}
                placeholder={t('editModal.scenesPh')}
              />
            </Form.Item>
            
            <Form.Item
              label={t('editModal.keyPointsLabel')}
              name="key_points"
              tooltip={t('editModal.keyPointsTooltip')}
              style={{ marginBottom: 12 }}
            >
              <TextArea
                rows={2}
                placeholder={t('editModal.keyPointsPh')}
              />
            </Form.Item>
            
            <Form.Item
              label={t('editModal.emotionLabel')}
              name="emotion"
              tooltip={t('editModal.emotionTooltip')}
              style={{ marginBottom: 12 }}
            >
              <Input placeholder={t('editModal.emotionPh')} />
            </Form.Item>
            
            <Form.Item
              label={t('editModal.goalLabel')}
              name="goal"
              tooltip={t('editModal.goalTooltip')}
              style={{ marginBottom: 0 }}
            >
              <Input placeholder={t('editModal.goalPh')} />
            </Form.Item>
          </Form>
        ),
        okText: t('editModal.okUpdate'),
        cancelText: t('editModal.cancel'),
        onOk: async () => {
          const values = await editForm.validateFields();
          try {
            // 解析并重构structure数据（使用预解析缓存，避免重复 JSON.parse）
            const originalStructure = outlineStructureMap[outline.id] || {};
            
            // 处理角色和组织数据 - 合并为带类型标识的新格式
            const charNames = Array.isArray(values.characters)
              ? values.characters.filter((c: string) => c && c.trim())
              : [];
            const orgNames = Array.isArray(values.organizations)
              ? values.organizations.filter((c: string) => c && c.trim())
              : [];
            const characters: CharacterEntry[] = [
              ...charNames.map((name: string) => ({ name: name.trim(), type: 'character' as const })),
              ...orgNames.map((name: string) => ({ name: name.trim(), type: 'organization' as const }))
            ];
            
            // 处理场景数据 - 检测原始格式
            let scenes: string[] | Array<{location: string; characters: string[]; purpose: string}> | undefined;
            if (values.scenes) {
              const lines = values.scenes.split('\n')
                .map((line: string) => line.trim())
                .filter((line: string) => line);
              
              // 检查是否包含管道符，判断格式
              const hasStructuredFormat = lines.some((line: string) => line.includes('|'));
              
              if (hasStructuredFormat) {
                // 尝试解析为对象数组格式
                scenes = lines
                  .map((line: string) => {
                    const parts = line.split('|');
                    if (parts.length >= 3) {
                      return {
                        location: parts[0].trim(),
                        characters: parts[1].split('、').map(c => c.trim()).filter(c => c),
                        purpose: parts[2].trim()
                      };
                    }
                    return null;
                  })
                  .filter((s: { location: string; characters: string[]; purpose: string } | null): s is { location: string; characters: string[]; purpose: string } => s !== null);
              } else {
                // 保持字符串数组格式
                scenes = lines;
              }
            }
            
            // 处理情节要点数据
            const keyPoints = values.key_points
              ? values.key_points.split('\n')
                  .map((line: string) => line.trim())
                  .filter((line: string) => line)
              : undefined;
            
            // 合并structure数据，只包含AI实际生成的字段
            const newStructure = {
              ...originalStructure,
              title: values.title,
              summary: values.content,
              characters: characters.length > 0 ? characters : undefined,
              scenes: scenes && scenes.length > 0 ? scenes : undefined,
              key_points: keyPoints && keyPoints.length > 0 ? keyPoints : undefined,
              emotion: values.emotion || undefined,
              goal: values.goal || undefined
            };
            
            // 更新大纲
            await updateOutline(id, {
              title: values.title,
              content: values.content,
              structure: JSON.stringify(newStructure, null, 2)
            });
            
            message.success(t('toast.updateSuccess'));
          } catch (error) {
            console.error('update outline failed:', error);
            message.error(t('toast.updateFailed'));
          }
        },
      });
    }
  };

  const handleDeleteOutline = async (id: string) => {
    try {
      await deleteOutline(id);
      message.success(t('toast.deleteSuccess'));
      // 删除后刷新大纲列表和项目信息，更新字数显示
      await refreshOutlines();
      if (currentProject?.id) {
        const updatedProject = await projectApi.getProject(currentProject.id);
        setCurrentProject(updatedProject);
      }
    } catch {
      message.error(t('toast.deleteFailed'));
    }
  };

  interface GenerateFormValues {
    theme?: string;
    chapter_count?: number;
    narrative_perspective?: string;
    requirements?: string;
    provider?: string;
    model?: string;
    mode?: 'auto' | 'new' | 'continue';
    story_direction?: string;
    plot_stage?: 'development' | 'climax' | 'ending';
    keep_existing?: boolean;
    content_language?: 'auto' | 'zh' | 'en';
  }

  const handleGenerate = async (values: GenerateFormValues) => {
    try {
      setIsGenerating(true);

      // 添加详细的调试日志
      console.log('=== outline generation debug ===');
      console.log('1. form values:', values);
      console.log('2. values.model:', values.model);
      console.log('3. values.provider:', values.provider);

      // 关闭生成表单Modal
      Modal.destroyAll();

      // 准备请求数据
      const requestData: OutlineGenerateRequestData = {
        project_id: currentProject.id,
        genre: currentProject.genre || '通用',
        theme: values.theme || currentProject.theme || '',
        chapter_count: values.chapter_count || 5,
        narrative_perspective: values.narrative_perspective || currentProject.narrative_perspective || '第三人称',
        target_words: currentProject.target_words || 100000,
        requirements: values.requirements,
        mode: values.mode || 'auto',
        story_direction: values.story_direction,
        plot_stage: values.plot_stage || 'development',
        // AI 生成内容语言（todo16：后端仅接受并存储，注入行为在 todo17/19 接入）
        content_language: values.content_language || 'auto'
      };

      // 只有在用户选择了模型时才添加model参数
      if (values.model) {
        requestData.model = values.model;
        console.log('4. adding model to request:', values.model);
      } else {
        console.log('4. values.model empty, not added to request');
      }

      // 添加provider参数（如果有）
      if (values.provider) {
        requestData.provider = values.provider;
        console.log('5. adding provider to request:', values.provider);
      }

      console.log('6. final request data:', JSON.stringify(requestData, null, 2));
      console.log('=========================');

      // 使用后台任务生成（不怕断连，关闭浏览器也继续运行）
      // 不再强制显示进度弹窗，任务进度在右下角悬浮任务框中显示
      await generateOutlineBackground(
        requestData,
        () => {
          // 进度更新由悬浮任务框处理，无需额外操作
        },
        (result) => {
          message.success(result.task_result?.message as string || t('generate.success'));
          setIsGenerating(false);
          refreshOutlines();
        },
        (error) => {
          message.error(t('generate.failed', { error }));
          setIsGenerating(false);
        }
      );

      message.info(t('generate.submitted'));
      // 通知悬浮任务框刷新
      eventBus.emit('background-task-created');

    } catch (error) {
      console.error('AI generation failed:', error);
      message.error(t('generate.aiFailed'));
      setIsGenerating(false);
    }
  };

  const showGenerateModal = async () => {
    const hasOutlines = outlines.length > 0;
    const initialMode = hasOutlines ? 'continue' : 'new';

    // 直接加载可用模型列表
    const settingsResponse = await fetch('/api/settings');
    const settings = await settingsResponse.json();
    const { api_key, api_base_url, api_provider } = settings;

    let loadedModels: Array<{ value: string, label: string }> = [];
    let defaultModel: string | undefined = undefined;

    if (api_base_url) {
      try {
        const modelsResponse = await fetch(
          `/api/settings/models?api_key=${encodeURIComponent(api_key || '')}&api_base_url=${encodeURIComponent(api_base_url)}&provider=${api_provider}`
        );
        if (modelsResponse.ok) {
          const data = await modelsResponse.json();
          if (data.models && data.models.length > 0) {
            loadedModels = data.models;
            defaultModel = settings.llm_model;
          }
        }
      } catch {
        console.log('fetch model list failed, using default model');
      }
    }

    modalApi.confirm({
      title: hasOutlines ? (
        <Space>
          <span>{t('generate.titleContinue')}</span>
          <Tag color="blue">{t('generate.hasOutlinesTag', { n: outlines.length })}</Tag>
        </Space>
      ) : t('generate.titleNew'),
      width: 700,
      centered: true,
      content: (
        <Form
          form={generateForm}
          layout="vertical"
          style={{ marginTop: 16 }}
          initialValues={{
            mode: initialMode,
            chapter_count: 5,
            narrative_perspective: currentProject.narrative_perspective || '第三人称',
            plot_stage: 'development',
            keep_existing: true,
            theme: currentProject.theme || '',
            model: defaultModel,
            content_language: 'auto',
          }}
        >
          {hasOutlines && (
            <Form.Item
              label={t('generate.labelMode')}
              name="mode"
              tooltip={t('generate.modeTooltip')}
            >
              <Radio.Group buttonStyle="solid">
                <Radio.Button value="auto">{t('generate.modeAuto')}</Radio.Button>
                <Radio.Button value="new">{t('generate.modeNew')}</Radio.Button>
                <Radio.Button value="continue">{t('generate.modeContinue')}</Radio.Button>
              </Radio.Group>
            </Form.Item>
          )}

          <Form.Item
            noStyle
            shouldUpdate={(prevValues, currentValues) => prevValues.mode !== currentValues.mode}
          >
            {({ getFieldValue }) => {
              const mode = getFieldValue('mode');
              const isContinue = mode === 'continue' || (mode === 'auto' && hasOutlines);

              // 续写模式不显示主题输入，使用项目原有主题
              if (isContinue) {
                return null;
              }

              // 全新生成模式需要输入主题
              return (
                <Form.Item
                  label={t('generate.labelTheme')}
                  name="theme"
                  rules={[{ required: true, message: t('generate.requiredTheme') }]}
                >
                  <TextArea rows={3} placeholder={t('generate.phTheme')} />
                </Form.Item>
              );
            }}
          </Form.Item>

          <Form.Item
            noStyle
            shouldUpdate={(prevValues, currentValues) => prevValues.mode !== currentValues.mode}
          >
            {({ getFieldValue }) => {
              const mode = getFieldValue('mode');
              const isContinue = mode === 'continue' || (mode === 'auto' && hasOutlines);

              return (
                <>
                  {isContinue && (
                    <>
                      <Form.Item
                        label={t('generate.directionLabel')}
                        name="story_direction"
                        tooltip={t('generate.directionTooltip')}
                      >
                        <TextArea
                          rows={3}
                          placeholder={t('generate.directionPh')}
                        />
                      </Form.Item>

                      <Form.Item
                        label={t('generate.labelPlotStage')}
                        name="plot_stage"
                        tooltip={t('generate.plotStageTooltip')}
                      >
                        <Select>
                          <Select.Option value="development">{t('generate.stageDevelopment')}</Select.Option>
                          <Select.Option value="climax">{t('generate.stageClimax')}</Select.Option>
                          <Select.Option value="ending">{t('generate.stageEnding')}</Select.Option>
                        </Select>
                      </Form.Item>
                    </>
                  )}

                  <Form.Item
                    label={isContinue ? t('generate.continueChapterCountLabel') : t('generate.chapterCountLabel')}
                    name="chapter_count"
                    rules={[{ required: true, message: t('generate.requiredChapterCount') }]}
                  >
                    <Input
                      type="number"
                      min={1}
                      max={50}
                      placeholder={isContinue ? t('generate.phCountContinue') : t('generate.phCountNew')}
                    />
                  </Form.Item>

                  <Form.Item
                    label={t('generate.labelPerspective')}
                    name="narrative_perspective"
                    rules={[{ required: true, message: t('generate.requiredPerspective') }]}
                  >
                    <Select>
                      <Select.Option value="第一人称">{t('perspective.firstPerson')}</Select.Option>
                      <Select.Option value="第三人称">{t('perspective.thirdPerson')}</Select.Option>
                      <Select.Option value="全知视角">{t('perspective.omniscient')}</Select.Option>
                    </Select>
                  </Form.Item>

                  <Form.Item label={t('generate.labelOther')} name="requirements">
                    <TextArea rows={2} placeholder={t('generate.phOther')} />
                  </Form.Item>

                </>
              );
            }}
          </Form.Item>

          {/* 生成内容语言（默认跟随界面语言；todo16 仅透传，注入行为在 todo17/19 接入） */}
          <Form.Item
            label={t('generate.contentLanguage.label')}
            name="content_language"
            style={{ marginTop: 12 }}
          >
            <Select
              options={[
                { value: 'auto', label: t('generate.contentLanguage.auto') },
                { value: 'zh', label: t('generate.contentLanguage.zh') },
                { value: 'en', label: t('generate.contentLanguage.en') },
              ]}
            />
          </Form.Item>

          {/* 自定义模型选择 - 移到外层，所有模式都显示 */}
          {loadedModels.length > 0 && (
            <Form.Item
              label={t('generate.modelLabel')}
              name="model"
              tooltip={t('generate.modelTooltip')}
            >
              <Select
                placeholder={defaultModel ? t('generate.modelSelected', { model: loadedModels.find(m => m.value === defaultModel)?.label || defaultModel }) : t('generate.modelPlaceholder')}
                allowClear
                showSearch
                optionFilterProp="label"
                options={loadedModels}
                onChange={(value) => {
                  console.log('user selected model:', value);
                  // 手动同步到Form
                  generateForm.setFieldsValue({ model: value });
                  console.log('synced to form, current values:', generateForm.getFieldsValue());
                }}
              />
              <div style={{ color: token.colorTextTertiary, fontSize: 12, marginTop: 4 }}>
                {defaultModel ? t('generate.defaultModelLabel', { model: loadedModels.find(m => m.value === defaultModel)?.label || defaultModel }) : t('generate.noDefaultModel')}
              </div>
            </Form.Item>
          )}
        </Form>
      ),
      okText: hasOutlines ? t('generate.okContinue') : t('generate.okGenerate'),
      cancelText: t('generate.cancel'),
      onOk: async () => {
        const values = await generateForm.validateFields();
        await handleGenerate(values);
      },
    });
  };

  // 手动创建大纲
  const showManualCreateOutlineModal = () => {
    const nextOrderIndex = outlines.length > 0
      ? Math.max(...outlines.map(o => o.order_index)) + 1
      : 1;

    modalApi.confirm({
      title: t('manual.title'),
      width: 600,
      centered: true,
      content: (
        <Form
          form={manualCreateForm}
          layout="vertical"
          initialValues={{ order_index: nextOrderIndex }}
          style={{ marginTop: 16 }}
        >
          <Form.Item
            label={t('manual.indexLabel')}
            name="order_index"
            rules={[{ required: true, message: t('manual.requiredIndex') }]}
            tooltip={currentProject?.outline_mode === 'one-to-one' ? t('manual.tooltipIndexOne') : t('manual.tooltipIndexMany')}
          >
            <InputNumber min={1} style={{ width: '100%' }} placeholder={t('manual.phIndex')} />
          </Form.Item>

          <Form.Item
            label={t('manual.titleLabel')}
            name="title"
            rules={[{ required: true, message: t('manual.requiredTitle') }]}
          >
            <Input placeholder={currentProject?.outline_mode === 'one-to-one' ? t('manual.phTitleOne') : t('manual.phTitleMany')} />
          </Form.Item>

          <Form.Item
            label={t('manual.contentLabel')}
            name="content"
            rules={[{ required: true, message: t('manual.contentRequired') }]}
          >
            <TextArea
              rows={6}
              placeholder={t('manual.contentPh')}
            />
          </Form.Item>
        </Form>
      ),
      okText: t('manual.create'),
      cancelText: t('manual.cancel'),
      onOk: async () => {
        const values = await manualCreateForm.validateFields();

        // 校验序号是否重复
        const existingOutline = outlines.find(o => o.order_index === values.order_index);
        if (existingOutline) {
          modalApi.warning({
            title: t('manual.conflictTitle'),
            content: (
              <div>
                <p><Trans ns="outline" i18nKey="manual.conflictUsed" components={{ strong: <strong /> }} values={{ order: values.order_index }} /></p>
                <div style={{
                  padding: 12,
                  background: token.colorWarningBg,
                  borderRadius: token.borderRadius,
                  border: `1px solid ${token.colorWarningBorder}`,
                  marginTop: 8
                }}>
                  <div style={{ fontWeight: 500, color: token.colorWarning }}>
                    {currentProject?.outline_mode === 'one-to-one'
                      ? t('manual.chapterNo', { order: existingOutline.order_index })
                      : t('manual.volumeNo', { order: existingOutline.order_index })
                    }：{existingOutline.title}
                  </div>
                </div>
                <p style={{ marginTop: 12, color: token.colorTextSecondary }}>
                  <Trans ns="outline" i18nKey="manual.suggestIndex" components={{ strong: <strong /> }} values={{ order: nextOrderIndex }} />
                </p>
              </div>
            ),
            okText: t('manual.iKnow'),
            centered: true
          });
          throw new Error(t('manual.duplicateIndex'));
        }

        try {
          await outlineApi.createOutline({
            project_id: currentProject.id,
            ...values
          });
          message.success(t('toast.createSuccess'));
          await refreshOutlines();
          manualCreateForm.resetFields();
        } catch (error: unknown) {
          const err = error as Error;
          if (err.message === t('manual.duplicateIndex')) {
            // 序号重复错误已经显示了Modal，不需要再显示message
            throw error;
          }
          message.error(t('toast.createFailed', { error: err.message || t('label.unknownError') }));
          throw error;
        }
      }
    });
  };

  // 展开单个大纲为多章 - 提交后台任务并在悬浮任务面板显示进度
  const handleExpandOutline = async (outlineId: string, outlineTitle: string) => {
    try {
      setIsExpanding(true);

      // ✅ 新增：检查是否需要按顺序展开
      const currentOutline = sortedOutlines.find(o => o.id === outlineId);
      if (currentOutline) {
        // 获取所有在当前大纲之前的大纲
        const previousOutlines = sortedOutlines.filter(
          o => o.order_index < currentOutline.order_index
        );

        // 检查前面的大纲是否都已展开
        for (const prevOutline of previousOutlines) {
          try {
            const prevChapters = await outlineApi.getOutlineChapters(prevOutline.id);
            if (!prevChapters.has_chapters) {
              // 如果前面有未展开的大纲，显示提示并阻止操作
              setIsExpanding(false);
              modalApi.warning({
                title: t('expand.orderWarnTitle'),
                width: 600,
                centered: true,
                content: (
                  <div>
                    <p style={{ marginBottom: 12 }}>
                      {t('expand.orderWarnBody')}
                    </p>
                    <div style={{
                      padding: 12,
                      background: token.colorWarningBg,
                      borderRadius: token.borderRadius,
                      border: `1px solid ${token.colorWarningBorder}`
                    }}>
                      <div style={{ fontWeight: 500, marginBottom: 8, color: token.colorWarning }}>
                        {t('expand.orderWarnNeed')}
                      </div>
                      <div style={{ color: token.colorTextSecondary }}>
                        {t('expand.orderWarnOutline', { order: prevOutline.order_index, title: prevOutline.title })}
                      </div>
                    </div>
                    <p style={{ marginTop: 12, color: token.colorTextSecondary, fontSize: 13 }}>
                      {t('expand.orderWarnTip')}
                    </p>
                  </div>
                ),
                okText: t('expand.iKnow')
              });
              return;
            }
          } catch (error) {
            console.error(`check outline ${prevOutline.id} failed:`, error);
            // 如果检查失败，继续处理（避免因网络问题阻塞）
          }
        }
      }

      // 第一步：检查是否已有展开的章节
      const existingChapters = await outlineApi.getOutlineChapters(outlineId);

      if (existingChapters.has_chapters && existingChapters.expansion_plans && existingChapters.expansion_plans.length > 0) {
        // 如果已有章节，显示已有的展开规划信息
        setIsExpanding(false);
        showExistingExpansionPreview(outlineTitle, existingChapters);
        return;
      }

      // 如果没有章节，显示展开表单
      setIsExpanding(false);
      modalApi.confirm({
        title: (
          <Space>
            <BranchesOutlined />
            <span>{t('expand.title')}</span>
          </Space>
        ),
        width: 600,
        centered: true,
        content: (
          <div>
            <div style={{ marginBottom: 16, padding: 12, background: token.colorBgLayout, borderRadius: token.borderRadius }}>
              <div style={{ fontWeight: 500, marginBottom: 4 }}>{t('expand.labelOutlineTitle')}</div>
              <div style={{ color: token.colorTextSecondary }}>{outlineTitle}</div>
            </div>
            <Form
              form={expansionForm}
              layout="vertical"
              initialValues={{
                target_chapter_count: 3,
                expansion_strategy: 'balanced',
              }}
            >
              <Form.Item
                label={t('expand.labelGoalCount')}
                name="target_chapter_count"
                rules={[{ required: true, message: t('expand.requiredGoalCount') }]}
                tooltip={t('expand.tooltipGoalCount')}
              >
                <InputNumber
                  min={2}
                  max={10}
                  style={{ width: '100%' }}
                  placeholder={t('expand.phGoalCount')}
                />
              </Form.Item>

              <Form.Item
                label={t('expand.strategyLabel')}
                name="expansion_strategy"
                tooltip={t('expand.strategyTooltip')}
              >
                <Radio.Group>
                  <Radio.Button value="balanced">{t('expand.strategyBalanced')}</Radio.Button>
                  <Radio.Button value="climax">{t('expand.strategyClimax')}</Radio.Button>
                  <Radio.Button value="detail">{t('expand.strategyDetail')}</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Form>
          </div>
        ),
        okText: t('expand.submit'),
        cancelText: t('expand.cancel'),
        onOk: async () => {
          try {
            const values = await expansionForm.validateFields();

            Modal.destroyAll();
            setIsExpanding(true);

            const requestData = {
              ...values,
              auto_create_chapters: true,
              enable_scene_analysis: true
            };

            const response = await fetch(`/api/outlines/${outlineId}/expand-background`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(requestData),
            });

            if (!response.ok) {
              const err = await response.json().catch(() => ({ detail: response.statusText }));
              throw new Error(err.detail || t('expand.contentFailed'));
            }

            message.success(t('expand.submitted'));
            eventBus.emit('background-task-created');
            setIsExpanding(false);

          } catch (error) {
            console.error('expand failed:', error);
            message.error(error instanceof Error ? error.message : t('expand.failedToast'));
            setIsExpanding(false);
          }
        },
      });
    } catch (error) {
      console.error('check chapters failed:', error);
      message.error(t('expand.checkChaptersFailed'));
      setIsExpanding(false);
    }
  };

  // 删除展开的章节内容（保留大纲）
  const handleDeleteExpandedChapters = async (outlineTitle: string, chapters: Array<{ id: string }>) => {
    try {
      // 使用顺序删除避免并发导致的字数计算竞态条件
      // 并发删除会导致多个请求同时读取项目字数并各自减去章节字数，造成计算错误
      for (const chapter of chapters) {
        await chapterApi.deleteChapter(chapter.id);
      }

      message.success(t('existing.deletedCount', { title: outlineTitle, n: chapters.length }));
      await refreshOutlines();
      // 刷新项目信息以更新字数显示
      if (currentProject?.id) {
        const updatedProject = await projectApi.getProject(currentProject.id);
        setCurrentProject(updatedProject);
      }
    } catch (error: unknown) {
      const apiError = error as ApiError;
      message.error(apiError.response?.data?.detail || t('existing.deleteFailed'));
    }
  };

  // 显示已存在章节的展开规划
  const showExistingExpansionPreview = (
    outlineTitle: string,
    data: {
      chapter_count: number;
      chapters: Array<{ id: string; chapter_number: number; title: string }>;
      expansion_plans: Array<{
        sub_index: number;
        title: string;
        plot_summary: string;
        key_events: string[];
        character_focus: string[];
        emotional_tone: string;
        narrative_goal: string;
        conflict_type: string;
        estimated_words: number;
        scenes?: Array<{
          location: string;
          characters: string[];
          purpose: string;
        }> | null;
      }> | null;
    }
  ) => {
    modalApi.info({
      title: (
        <Space style={{ flexWrap: 'wrap' }}>
          <CheckCircleOutlined style={{ color: token.colorSuccess }} />
          <span>{t('existing.infoTitle', { title: outlineTitle })}</span>
        </Space>
      ),
      width: isMobile ? '95%' : 900,
      centered: true,
      style: isMobile ? {
        top: 20,
        maxWidth: 'calc(100vw - 16px)',
        margin: '0 8px'
      } : undefined,
      styles: {
        body: {
          maxHeight: isMobile ? 'calc(100vh - 200px)' : 'calc(80vh - 60px)',
          overflowY: 'auto',
          overflowX: 'hidden'
        }
      },
      footer: (
        <Space wrap style={{ width: '100%', justifyContent: isMobile ? 'center' : 'flex-end' }}>
          <Button
            danger
            icon={<DeleteOutlined />}
            onClick={() => {
              Modal.destroyAll();
              modalApi.confirm({
                title: t('existing.confirmTitle'),
                icon: <ExclamationCircleOutlined />,
                centered: true,
                content: (
                  <div>
                    <p><Trans ns="outline" i18nKey="existing.confirmContent" components={{ strong: <strong /> }} values={{ title: outlineTitle, chapters: data.chapter_count }} /></p>
                    <p style={{ color: token.colorPrimary, marginTop: 8 }}>
                      {t('existing.confirmNote')}
                    </p>
                    <p style={{ color: token.colorError, marginTop: 8 }}>
                      {t('existing.confirmWarn')}
                    </p>
                  </div>
                ),
                okText: t('existing.confirmOk'),
                okType: 'danger',
                cancelText: t('existing.confirmCancel'),
                onOk: () => handleDeleteExpandedChapters(outlineTitle, data.chapters || []),
              });
            }}
            block={isMobile}
            size={isMobile ? 'middle' : undefined}
          >
            {t('existing.deleteAllBtn', { n: data.chapter_count })}
          </Button>
          <Button onClick={() => Modal.destroyAll()}>
            {t('existing.close')}
          </Button>
        </Space>
      ),
      content: (
        <div>
          <div style={{ marginBottom: 16 }}>
            <Space wrap style={{ maxWidth: '100%' }}>
              <Tag
                color="blue"
                style={{
                  whiteSpace: 'normal',
                  wordBreak: 'break-word',
                  height: 'auto',
                  lineHeight: '1.5',
                  padding: '4px 8px'
                }}
              >
                {t('existing.infoOutlinePrefix')}{outlineTitle}
              </Tag>
              <Tag color="green">{t('existing.countTag', { n: data.chapter_count })}</Tag>
              <Tag color="orange">{t('existing.createdTag')}</Tag>
            </Space>
          </div>
          <Tabs
            defaultActiveKey="0"
            type="card"
            items={data.expansion_plans?.map((plan, idx) => ({
              key: idx.toString(),
              label: (
                <Space size="small" style={{ maxWidth: isMobile ? '150px' : 'none' }}>
                  <span
                    style={{
                      fontWeight: 500,
                      whiteSpace: isMobile ? 'normal' : 'nowrap',
                      wordBreak: isMobile ? 'break-word' : 'normal',
                      fontSize: isMobile ? 12 : 14
                    }}
                  >
                    {plan.sub_index}. {plan.title}
                  </span>
                </Space>
              ),
              children: (
                <div style={{ maxHeight: '500px', overflowY: 'auto', padding: '8px 0' }}>
                  <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                    <Card size="small" title={t('existing.tabBasic')}>
                      <Space wrap style={{ maxWidth: '100%' }}>
                        <Tag
                          color="blue"
                          style={{
                            whiteSpace: 'normal',
                            wordBreak: 'break-word',
                            height: 'auto',
                            lineHeight: '1.5',
                            padding: '4px 8px'
                          }}
                        >
                          {plan.emotional_tone}
                        </Tag>
                        <Tag
                          color="orange"
                          style={{
                            whiteSpace: 'normal',
                            wordBreak: 'break-word',
                            height: 'auto',
                            lineHeight: '1.5',
                            padding: '4px 8px'
                          }}
                        >
                          {plan.conflict_type}
                        </Tag>
                        <Tag color="green">{t('existing.infoWordsTag', { n: plan.estimated_words })}</Tag>
                      </Space>
                    </Card>

                    <Card size="small" title={t('existing.tabPlot')}>
                      <div style={{
                        wordBreak: 'break-word',
                        whiteSpace: 'normal',
                        overflowWrap: 'break-word'
                      }}>
                        {plan.plot_summary}
                      </div>
                    </Card>

                    <Card size="small" title={t('existing.tabGoal')}>
                      <div style={{
                        wordBreak: 'break-word',
                        whiteSpace: 'normal',
                        overflowWrap: 'break-word'
                      }}>
                        {plan.narrative_goal}
                      </div>
                    </Card>

                    <Card size="small" title={t('existing.tabEvents')}>
                      <Space direction="vertical" size="small" style={{ width: '100%' }}>
                        {plan.key_events.map((event, eventIdx) => (
                          <div
                            key={eventIdx}
                            style={{
                              wordBreak: 'break-word',
                              whiteSpace: 'normal',
                              overflowWrap: 'break-word'
                            }}
                          >
                            • {event}
                          </div>
                        ))}
                      </Space>
                    </Card>

                    <Card size="small" title={t('existing.tabChars')}>
                      <Space wrap style={{ maxWidth: '100%' }}>
                        {plan.character_focus.map((char, charIdx) => (
                          <Tag
                            key={charIdx}
                            color="purple"
                            style={{
                              whiteSpace: 'normal',
                              wordBreak: 'break-word',
                              height: 'auto',
                              lineHeight: '1.5'
                            }}
                          >
                            {char}
                          </Tag>
                        ))}
                      </Space>
                    </Card>

                    {plan.scenes && plan.scenes.length > 0 && (
                      <Card size="small" title={t('existing.tabScenes')}>
                        <Space direction="vertical" size="small" style={{ width: '100%' }}>
                          {plan.scenes.map((scene, sceneIdx) => (
                            <Card
                              key={sceneIdx}
                              size="small"
                              style={{
                                backgroundColor: token.colorFillQuaternary,
                                maxWidth: '100%',
                                overflow: 'hidden'
                              }}
                            >
                              <div style={{
                                wordBreak: 'break-word',
                                whiteSpace: 'normal',
                                overflowWrap: 'break-word'
                              }}>
                                <strong>{t('existing.sceneLocationLabel')}</strong>{scene.location}
                              </div>
                              <div style={{
                                wordBreak: 'break-word',
                                whiteSpace: 'normal',
                                overflowWrap: 'break-word'
                              }}>
                                <strong>{t('existing.sceneCharsLabel')}</strong>{scene.characters.join(t('existing.listSep'))}
                              </div>
                              <div style={{
                                wordBreak: 'break-word',
                                whiteSpace: 'normal',
                                overflowWrap: 'break-word'
                              }}>
                                <strong>{t('existing.scenePurposeLabel')}</strong>{scene.purpose}
                              </div>
                            </Card>
                          ))}
                        </Space>
                      </Card>
                    )
                    }
                  </Space>
                </div >
              )
            }))}
          />
        </div >
      ),
    });
  };

  // 批量展开所有大纲 - 提交后台任务并在悬浮任务面板显示进度
  const handleBatchExpandOutlines = () => {
    if (!currentProject?.id || outlines.length === 0) {
      message.warning(t('batchExpand.noOutlines'));
      return;
    }

    modalApi.confirm({
      title: (
        <Space>
          <AppstoreAddOutlined />
          <span>{t('batchExpand.title')}</span>
        </Space>
      ),
      width: 600,
      centered: true,
      content: (
        <div>
          <div
            style={{
              marginBottom: 16,
              padding: 12,
              background: token.colorWarningBg,
              borderRadius: token.borderRadius,
              border: `1px solid ${token.colorWarningBorder}`,
            }}
          >
            <div style={{ color: token.colorWarningText }}>
              {t('batchExpand.warnAll', { n: outlines.length })}
            </div>
          </div>
          <Form
            form={batchExpansionForm}
            layout="vertical"
            initialValues={{
              chapters_per_outline: 3,
              expansion_strategy: 'balanced',
            }}
          >
            <Form.Item
              label={t('batchExpand.chaptersPerLabel')}
              name="chapters_per_outline"
              rules={[{ required: true, message: t('batchExpand.requiredChapters') }]}
              tooltip={t('batchExpand.chaptersPerTooltip')}
            >
              <InputNumber
                min={2}
                max={10}
                style={{ width: '100%' }}
                placeholder={t('batchExpand.phChapters')}
              />
            </Form.Item>

            <Form.Item
              label={t('batchExpand.strategyLabel')}
              name="expansion_strategy"
            >
              <Radio.Group>
                <Radio.Button value="balanced">{t('batchExpand.labelBalanced')}</Radio.Button>
                <Radio.Button value="climax">{t('batchExpand.labelClimax')}</Radio.Button>
                <Radio.Button value="detail">{t('batchExpand.labelDetail')}</Radio.Button>
              </Radio.Group>
            </Form.Item>
          </Form>
        </div>
      ),
      okText: t('batchExpand.submit'),
      cancelText: t('batchExpand.cancel'),
      okButtonProps: { type: 'primary' },
      onOk: async () => {
        try {
          const values = await batchExpansionForm.validateFields();

          Modal.destroyAll();
          setIsExpanding(true);

          const requestData = {
            project_id: currentProject.id,
            ...values,
            auto_create_chapters: true,
            enable_scene_analysis: true
          };

          const response = await fetch('/api/outlines/batch-expand-background', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestData),
          });

          if (!response.ok) {
            const err = await response.json().catch(() => ({ detail: response.statusText }));
            throw new Error(err.detail || t('batchExpand.failed'));
          }

          message.success(t('batchExpand.submitted'));
          eventBus.emit('background-task-created');
          setIsExpanding(false);

        } catch (error) {
          console.error('batch expand failed:', error);
          message.error(error instanceof Error ? error.message : t('batchExpand.failedToast'));
          setIsExpanding(false);
        }
      },
    });
  };

  const resetImportDialog = () => {
    setImportFile(null);
    setImportMode('append');
    setImportPreview(null);
    setIsPreviewingImport(false);
  };

  const handleExportOutlines = async () => {
    if (!currentProject?.id) return;
    setIsExporting(true);
    try {
      await outlineApi.exportOutlines(currentProject.id);
      message.success(t('export.done', { n: outlines.length }));
    } catch (error) {
      console.error('export outlines failed:', error);
      message.error(t('export.failed'));
    } finally {
      setIsExporting(false);
    }
  };

  const handlePreviewImport = async () => {
    if (!currentProject?.id || !importFile) {
      message.warning(t('import.selectFileFirst'));
      return;
    }

    setIsPreviewingImport(true);
    setImportPreview(null);
    try {
      const preview = await outlineApi.previewImport(currentProject.id, importMode, importFile);
      setImportPreview(preview);
    } catch (error) {
      console.error('preview outline import failed:', error);
    } finally {
      setIsPreviewingImport(false);
    }
  };

  const handleConfirmImport = async () => {
    if (!currentProject?.id || !importFile || !importPreview?.valid) return;

    setIsImporting(true);
    try {
      const result = await outlineApi.importOutlines(currentProject.id, importMode, importFile);
      const chapterMessage = result.created_chapters > 0
        ? t('import.chapterSuffix', { n: result.created_chapters })
        : '';
      message.success(`${result.message}${chapterMessage}`);
      setImportModalOpen(false);
      resetImportDialog();
      await refreshOutlines();
      if (result.created_chapters > 0) {
        eventBus.emit(EventNames.CHAPTER_NEEDS_REFRESH);
      }
    } catch (error) {
      console.error('import outlines failed:', error);
      setImportPreview(null);
    } finally {
      setIsImporting(false);
    }
  };

  return (
    <>
      {contextHolder}

      <Modal
        title={t('import.title')}
        open={importModalOpen}
        width={680}
        okText={t('import.confirmImport')}
        cancelText={t('import.cancel')}
        confirmLoading={isImporting}
        okButtonProps={{ disabled: !importPreview?.valid || isPreviewingImport }}
        maskClosable={!isImporting}
        closable={!isImporting}
        onOk={handleConfirmImport}
        onCancel={() => {
          if (isImporting) return;
          setImportModalOpen(false);
          resetImportDialog();
        }}
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message={t('import.tooltip')}
          />

          <div>
            <div style={{ marginBottom: 8, fontWeight: 500 }}>{t('import.phStep1')}</div>
            <Upload
              accept=".json,application/json"
              maxCount={1}
              fileList={importFile ? [{
                uid: 'outline-import-file',
                name: importFile.name,
                size: importFile.size,
                type: importFile.type,
                status: 'done',
              }] : []}
              beforeUpload={(file) => {
                if (!file.name.toLowerCase().endsWith('.json')) {
                  message.error(t('import.formatError'));
                  return Upload.LIST_IGNORE;
                }
                if (file.size > 10 * 1024 * 1024) {
                  message.error(t('import.fileTooLarge'));
                  return Upload.LIST_IGNORE;
                }
                setImportFile(file);
                setImportPreview(null);
                return false;
              }}
              onRemove={() => {
                setImportFile(null);
                setImportPreview(null);
                return true;
              }}
            >
              <Button icon={<UploadOutlined />}>{t('import.btnSelectJson')}</Button>
            </Upload>
          </div>

          <div>
            <div style={{ marginBottom: 8, fontWeight: 500 }}>{t('import.phStep2')}</div>
            <Radio.Group
              value={importMode}
              onChange={(event) => {
                setImportMode(event.target.value as OutlineImportMode);
                setImportPreview(null);
              }}
            >
              <Space direction="vertical">
                <Radio value="append">{t('import.radioAppend')}</Radio>
                <Radio value="merge">{t('import.radioMerge')}</Radio>
              </Space>
            </Radio.Group>
          </div>

          <Button
            type="primary"
            ghost
            icon={<FileTextOutlined />}
            disabled={!importFile}
            loading={isPreviewingImport}
            onClick={handlePreviewImport}
          >
            {t('import.previewBtn')}
          </Button>

          {importPreview && (
            <>
              <Divider style={{ margin: '4px 0' }} />
              <Alert
                type={importPreview.valid ? 'success' : 'error'}
                showIcon
                message={importPreview.valid ? t('import.validPassed') : t('import.validFailed')}
                description={
                  <Space direction="vertical" size={4} style={{ width: '100%' }}>
                    <div>
                      {t('import.versionLine', { version: importPreview.version || t('import.versionUnknown') })}
                      {importPreview.source_project?.title && t('import.sourceProject', { title: importPreview.source_project.title })}
                    </div>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                      <Tag>{t('import.statTotal', { n: importPreview.statistics.total })}</Tag>
                      <Tag color="green">{t('import.statCreate', { n: importPreview.statistics.will_create })}</Tag>
                      <Tag color="blue">{t('import.statUpdate', { n: importPreview.statistics.will_update })}</Tag>
                      {importPreview.target_outline_mode === 'one-to-one' && (
                        <Tag color="purple">{t('import.statCreateChapters', { n: importPreview.statistics.will_create_chapters })}</Tag>
                      )}
                    </div>
                  </Space>
                }
              />

              {importPreview.errors.length > 0 && (
                <Alert
                  type="error"
                  showIcon
                  message={t('import.cannotImport')}
                  description={
                    <ul style={{ margin: 0, paddingLeft: 20 }}>
                      {importPreview.errors.map((error, index) => <li key={`${index}-${error}`}>{error}</li>)}
                    </ul>
                  }
                />
              )}

              {importPreview.warnings.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  message={t('import.noticeTitle')}
                  description={
                    <ul style={{ margin: 0, paddingLeft: 20 }}>
                      {importPreview.warnings.map((warning, index) => <li key={`${index}-${warning}`}>{warning}</li>)}
                    </ul>
                  }
                />
              )}
            </>
          )}
        </Space>
      </Modal>

      <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
        {/* 固定头部 */}
        <div style={{
          position: 'sticky',
          top: 0,
          zIndex: 10,
          backgroundColor: token.colorBgContainer,
          padding: isMobile ? '12px 0' : '16px 0',
          marginBottom: isMobile ? 12 : 16,
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
          display: 'flex',
          flexDirection: isMobile ? 'column' : 'row',
          gap: isMobile ? 12 : 0,
          justifyContent: 'space-between',
          alignItems: isMobile ? 'stretch' : 'center'
        }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <h2 style={{ margin: 0, fontSize: isMobile ? 18 : 24 }}>
              <FileTextOutlined style={{ marginRight: 8 }} />
              {t('page.title')}
            </h2>
            {currentProject?.outline_mode && (
              <Tag color={currentProject.outline_mode === 'one-to-one' ? 'blue' : 'green'} style={{ width: 'fit-content' }}>
                {currentProject.outline_mode === 'one-to-one' ? t('page.modeOneToOne') : t('page.modeOneToMany')}
              </Tag>
            )}
          </div>
          <Space size="small" wrap={isMobile}>
            <Input.Search
              allowClear
              placeholder={t('page.searchPlaceholder')}
              value={outlineSearchKeyword}
              onChange={(e) => setOutlineSearchKeyword(e.target.value)}
              style={{ width: isMobile ? '100%' : 280 }}
            />
            <Button
              icon={<UploadOutlined />}
              onClick={() => {
                resetImportDialog();
                setImportModalOpen(true);
              }}
              block={isMobile}
            >
              {t('page.import')}
            </Button>
            <Button
              icon={<DownloadOutlined />}
              onClick={handleExportOutlines}
              loading={isExporting}
              disabled={outlines.length === 0}
              block={isMobile}
            >
              {t('page.export')}
            </Button>
            <Button
              icon={<PlusOutlined />}
              onClick={showManualCreateOutlineModal}
              block={isMobile}
            >
              {t('page.manualCreate')}
            </Button>
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              onClick={showGenerateModal}
              loading={isGenerating}
              block={isMobile}
            >
              {isMobile ? t('page.aiGenerateShort') : t('page.aiGenerate')}
            </Button>
            {outlines.length > 0 && currentProject?.outline_mode === 'one-to-many' && (
              <Button
                icon={<AppstoreAddOutlined />}
                onClick={handleBatchExpandOutlines}
                loading={isExpanding}
                disabled={isGenerating}
                title={t('page.batchExpandTooltip')}
              >
                {isMobile ? t('page.batchExpandShort') : t('page.batchExpand')}
              </Button>
            )}
          </Space>
        </div>

        {/* 可滚动内容区域 */}
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {outlines.length === 0 ? (
            <Empty description={t('page.emptyNone')} />
          ) : filteredOutlines.length === 0 ? (
            <Empty description={t('page.emptyNoMatch')} />
          ) : (
            <List
              dataSource={pagedOutlines}
              renderItem={(item) => {
                  const structureData = outlineStructureMap[item.id] || {};

                  // 解析角色/组织条目（兼容新旧格式）
                  const characterEntries = parseCharacterEntries(structureData.characters);
                  const characterNames = getCharacterNames(characterEntries);
                  const organizationNames = getOrganizationNames(characterEntries);
                  const isOutlineExpanded = outlineContentExpandStatus[item.id] || false;
                  const previewContent = getOutlinePreview(item.content, isMobile ? 70 : 140);
                  
                  return (
                    <List.Item
                      style={{
                        marginBottom: 16,
                        padding: 0,
                        border: 'none'
                      }}
                    >
                      <Card
                        style={{
                          width: '100%',
                          borderRadius: isMobile ? 6 : 8,
                          border: `1px solid ${token.colorBorderSecondary}`,
                          boxShadow: `0 1px 2px ${alphaColor(token.colorTextBase, 0.08)}`,
                          transition: 'all 0.3s ease'
                        }}
                        bodyStyle={{
                          padding: isMobile ? '10px 12px' : 16
                        }}
                        onMouseEnter={(e) => {
                          if (!isMobile) {
                            e.currentTarget.style.boxShadow = `0 4px 12px ${alphaColor(token.colorTextBase, 0.16)}`;
                            e.currentTarget.style.borderColor = token.colorPrimary;
                          }
                        }}
                        onMouseLeave={(e) => {
                          if (!isMobile) {
                            e.currentTarget.style.boxShadow = `0 1px 2px ${alphaColor(token.colorTextBase, 0.08)}`;
                            e.currentTarget.style.borderColor = token.colorBorderSecondary;
                          }
                        }}
                      >
                        <List.Item.Meta
                          style={{ width: '100%' }}
                          title={
                            <Space size="small" style={{ fontSize: isMobile ? 13 : 16, flexWrap: 'wrap', lineHeight: isMobile ? '1.4' : '1.5' }}>
                              <span style={{ color: token.colorPrimary, fontWeight: 'bold', fontSize: isMobile ? 13 : 16 }}>
                                {currentProject?.outline_mode === 'one-to-one'
                                  ? t('listItem.chapterNo', { n: item.order_index || '?' })
                                  : t('listItem.volumeNo', { n: item.order_index || '?' })
                                }
                              </span>
                              <span style={{ fontSize: isMobile ? 13 : 16 }}>{item.title}</span>
                              {/* ✅ 新增：展开状态标识 - 仅在一对多模式显示 */}
                              {currentProject?.outline_mode === 'one-to-many' && (
                                outlineExpandStatus[item.id] ? (
                                  <Tag color="success" icon={<CheckCircleOutlined />} style={{ fontSize: isMobile ? 11 : 12 }}>{t('listItem.expanded')}</Tag>
                                ) : (
                                  <Tag color="default" style={{ fontSize: isMobile ? 11 : 12 }}>{t('listItem.notExpanded')}</Tag>
                                )
                              )}
                            </Space>
                          }
                          description={
                            <div style={{ fontSize: isMobile ? 12 : 14, lineHeight: isMobile ? '1.5' : '1.6' }}>
                              {/* 大纲内容 */}
                              <div style={{
                                marginBottom: isMobile ? 10 : 12,
                                padding: isMobile ? '8px 10px' : '10px 12px',
                                background: token.colorFillQuaternary,
                                borderLeft: `3px solid ${token.colorBorderSecondary}`,
                                borderRadius: token.borderRadius,
                                fontSize: isMobile ? 12 : 13,
                                color: token.colorText,
                                lineHeight: '1.6'
                              }}>
                                <div style={{
                                  display: 'flex',
                                  alignItems: 'center',
                                  justifyContent: 'space-between',
                                  gap: 8,
                                  marginBottom: isMobile ? 4 : 6,
                                  flexWrap: isMobile ? 'wrap' : 'nowrap'
                                }}>
                                  <div style={{
                                    fontWeight: 600,
                                    color: token.colorTextSecondary,
                                    fontSize: isMobile ? 12 : 13
                                  }}>
                                    {t('page.outlineContentLabel')}
                                  </div>
                                  <Button
                                    type="link"
                                    size="small"
                                    onClick={() => setOutlineContentExpandStatus(prev => ({
                                      ...prev,
                                      [item.id]: !isOutlineExpanded
                                    }))}
                                    style={{
                                      padding: 0,
                                      height: 'auto',
                                      fontSize: isMobile ? 12 : 13
                                    }}
                                  >
                                    {isOutlineExpanded ? t('page.collapse') : t('page.expand')}
                                  </Button>
                                </div>
                                <div style={{
                                  padding: isMobile ? '6px 8px' : '6px 10px',
                                  background: token.colorBgContainer,
                                  border: `1px solid ${token.colorBorder}`,
                                  borderRadius: token.borderRadiusSM,
                                  fontSize: isMobile ? 12 : 13,
                                  color: token.colorText,
                                  lineHeight: '1.8',
                                  whiteSpace: isOutlineExpanded ? 'pre-wrap' : 'normal',
                                  wordBreak: 'break-word'
                                }}>
                                  {isOutlineExpanded ? item.content : previewContent.text || t('page.noContent')}
                                </div>
                              </div>

                              {isOutlineExpanded && (
                                <>
                              {/* ✨ 涉及角色展示 - 优化版（支持角色/组织分类显示） */}
                              {characterNames.length > 0 && (
                                <div style={{
                                  marginTop: isMobile ? 10 : 12,
                                  padding: isMobile ? '8px 10px' : '10px 12px',
                                  background: token.colorPrimaryBg,
                                  borderLeft: `3px solid ${token.colorPrimary}`,
                                  borderRadius: token.borderRadius
                                }}>
                                  <div style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: isMobile ? 6 : 8,
                                    marginBottom: isMobile ? 6 : 8
                                  }}>
                                    <span style={{
                                      fontSize: isMobile ? 12 : 13,
                                      fontWeight: 600,
                                      color: token.colorPrimary,
                                      display: 'flex',
                                      alignItems: 'center',
                                      gap: 4
                                    }}>
                                      {t('page.charLabel')}
                                      <Tag
                                        color="purple"
                                        style={{
                                          margin: 0,
                                          fontSize: 10,
                                          borderRadius: 10,
                                          padding: '0 6px'
                                        }}
                                      >
                                        {characterNames.length}
                                      </Tag>
                                    </span>
                                  </div>
                                  <Space wrap size={[4, 4]}>
                                    {characterNames.map((name, idx) => (
                                      <Tag
                                        key={idx}
                                        color="purple"
                                        style={{
                                          margin: 0,
                                          borderRadius: 4,
                                          padding: isMobile ? '2px 8px' : '3px 10px',
                                          fontSize: isMobile ? 11 : 12,
                                          fontWeight: 500,
                                          border: `1px solid ${token.colorPrimaryBorder}`,
                                          background: token.colorBgContainer,
                                          color: token.colorPrimary,
                                          whiteSpace: 'normal',
                                          wordBreak: 'break-word',
                                          height: 'auto',
                                          lineHeight: '1.5'
                                        }}
                                      >
                                        {name}
                                      </Tag>
                                    ))}
                                  </Space>
                                </div>
                              )}
                              
                              {/* 🏛️ 涉及组织展示 */}
                              {organizationNames.length > 0 && (
                                <div style={{
                                  marginTop: isMobile ? 10 : 12,
                                  padding: isMobile ? '8px 10px' : '10px 12px',
                                  background: token.colorWarningBg,
                                  borderLeft: `3px solid ${token.colorWarning}`,
                                  borderRadius: token.borderRadius
                                }}>
                                  <div style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: isMobile ? 6 : 8,
                                    marginBottom: isMobile ? 6 : 8
                                  }}>
                                    <span style={{
                                      fontSize: isMobile ? 12 : 13,
                                      fontWeight: 600,
                                      color: token.colorWarning,
                                      display: 'flex',
                                      alignItems: 'center',
                                      gap: 4
                                    }}>
                                      {t('page.orgLabel')}
                                      <Tag
                                        color="orange"
                                        style={{
                                          margin: 0,
                                          fontSize: 10,
                                          borderRadius: 10,
                                          padding: '0 6px'
                                        }}
                                      >
                                        {organizationNames.length}
                                      </Tag>
                                    </span>
                                  </div>
                                  <Space wrap size={[4, 4]}>
                                    {organizationNames.map((name, idx) => (
                                      <Tag
                                        key={idx}
                                        color="orange"
                                        style={{
                                          margin: 0,
                                          borderRadius: 4,
                                          padding: isMobile ? '2px 8px' : '3px 10px',
                                          fontSize: isMobile ? 11 : 12,
                                          fontWeight: 500,
                                          border: `1px solid ${token.colorWarningBorder}`,
                                          background: token.colorBgContainer,
                                          color: token.colorWarning,
                                          whiteSpace: 'normal',
                                          wordBreak: 'break-word',
                                          height: 'auto',
                                          lineHeight: '1.5'
                                        }}
                                      >
                                        {name}
                                      </Tag>
                                    ))}
                                  </Space>
                                </div>
                              )}
                              
                              {/* ✨ 场景信息展示 - 优化版（支持折叠，最多显示3个） */}
                              {structureData.scenes && structureData.scenes.length > 0 ? (() => {
                                const isExpanded = scenesExpandStatus[item.id] || false;
                                const maxVisibleScenes = 4;
                                const hasMoreScenes = structureData.scenes!.length > maxVisibleScenes;
                                const visibleScenes = isExpanded ? structureData.scenes : structureData.scenes!.slice(0, maxVisibleScenes);
                                
                                return (
                                  <div style={{
                                    marginTop: isMobile ? 10 : 12,
                                    padding: isMobile ? '8px 10px' : '10px 12px',
                                    background: token.colorInfoBg,
                                    borderLeft: `3px solid ${token.colorInfo}`,
                                    borderRadius: token.borderRadius
                                  }}>
                                    <div style={{
                                      display: 'flex',
                                      alignItems: 'center',
                                      justifyContent: 'space-between',
                                      marginBottom: isMobile ? 6 : 8,
                                      flexWrap: isMobile ? 'wrap' : 'nowrap',
                                      gap: isMobile ? 4 : 0
                                    }}>
                                      <span style={{
                                        fontSize: isMobile ? 12 : 13,
                                        fontWeight: 600,
                                        color: token.colorInfo,
                                        display: 'flex',
                                        alignItems: 'center',
                                        gap: 4
                                      }}>
                                        {t('page.scenesLabel')}
                                        <Tag
                                          color="cyan"
                                          style={{
                                            margin: 0,
                                            fontSize: 10,
                                            borderRadius: 10,
                                            padding: '0 6px'
                                          }}
                                        >
                                          {structureData.scenes!.length}
                                        </Tag>
                                      </span>
                                      {hasMoreScenes && (
                                        <Button
                                          type="text"
                                          size="small"
                                          onClick={() => setScenesExpandStatus(prev => ({
                                            ...prev,
                                            [item.id]: !isExpanded
                                          }))}
                                          style={{
                                            fontSize: isMobile ? 10 : 11,
                                            height: isMobile ? 20 : 22,
                                            padding: isMobile ? '0 6px' : '0 8px',
                                            color: token.colorInfo
                                          }}
                                        >
                                          {isExpanded ? t('page.collapseWithIcon') : t('page.expandMore', { n: structureData.scenes!.length - maxVisibleScenes })}
                                        </Button>
                                      )}
                                    </div>
                                    {/* 使用grid布局，移动端一列，桌面端两列 */}
                                    <div style={{
                                      display: 'grid',
                                      gridTemplateColumns: isMobile ? '1fr' : 'repeat(auto-fill, minmax(280px, 1fr))',
                                      gap: isMobile ? 6 : 8,
                                      width: '100%',
                                      minWidth: 0  // 防止grid子元素溢出
                                    }}>
                                      {visibleScenes!.map((scene, idx) => {
                                      // 判断是字符串还是对象
                                      if (typeof scene === 'string') {
                                        // 字符串格式：简洁卡片
                                        return (
                                          <div
                                            key={idx}
                                            style={{
                                              padding: isMobile ? '6px 8px' : '8px 10px',
                                              background: token.colorBgContainer,
                                              border: `1px solid ${token.colorInfoBorder}`,
                                              borderRadius: token.borderRadius,
                                              fontSize: isMobile ? 11 : 12,
                                              color: token.colorText,
                                              display: 'flex',
                                              alignItems: 'flex-start',
                                              gap: isMobile ? 6 : 8,
                                              transition: 'all 0.2s ease',
                                              cursor: 'default',
                                              width: '100%',
                                              minWidth: 0,
                                              boxSizing: 'border-box'
                                            }}
                                            onMouseEnter={(e) => {
                                              if (!isMobile) {
                                                e.currentTarget.style.borderColor = token.colorInfo;
                                                e.currentTarget.style.boxShadow = `0 2px 8px ${alphaColor(token.colorInfo, 0.25)}`;
                                              }
                                            }}
                                            onMouseLeave={(e) => {
                                              if (!isMobile) {
                                                e.currentTarget.style.borderColor = token.colorInfoBorder;
                                                e.currentTarget.style.boxShadow = 'none';
                                              }
                                            }}
                                          >
                                            <Tag
                                              color="cyan"
                                              style={{
                                                margin: 0,
                                                fontSize: 10,
                                                borderRadius: 4,
                                                flexShrink: 0
                                              }}
                                            >
                                              {idx + 1}
                                            </Tag>
                                            <span style={{
                                              flex: 1,
                                              lineHeight: '1.6',
                                              overflow: 'hidden',
                                              textOverflow: 'ellipsis',
                                              whiteSpace: 'nowrap'
                                            }}>{scene}</span>
                                          </div>
                                        );
                                      } else {
                                        // 对象格式：详细卡片
                                        return (
                                          <div
                                            key={idx}
                                            style={{
                                              padding: isMobile ? '8px 10px' : '10px 12px',
                                              background: token.colorBgContainer,
                                              border: `1px solid ${token.colorInfoBorder}`,
                                              borderRadius: token.borderRadius,
                                              fontSize: isMobile ? 11 : 12,
                                              transition: 'all 0.2s ease',
                                              cursor: 'default',
                                              width: '100%',
                                              minWidth: 0,
                                              boxSizing: 'border-box'
                                            }}
                                            onMouseEnter={(e) => {
                                              if (!isMobile) {
                                                e.currentTarget.style.borderColor = token.colorInfo;
                                                e.currentTarget.style.boxShadow = `0 2px 8px ${alphaColor(token.colorInfo, 0.25)}`;
                                              }
                                            }}
                                            onMouseLeave={(e) => {
                                              if (!isMobile) {
                                                e.currentTarget.style.borderColor = token.colorInfoBorder;
                                                e.currentTarget.style.boxShadow = 'none';
                                              }
                                            }}
                                          >
                                            <div style={{
                                              display: 'flex',
                                              alignItems: 'center',
                                              gap: isMobile ? 6 : 8,
                                              marginBottom: isMobile ? 4 : 6,
                                              flexWrap: 'wrap'
                                            }}>
                                              <Tag
                                                color="cyan"
                                                style={{
                                                  margin: 0,
                                                  fontSize: 10,
                                                  borderRadius: 4
                                                }}
                                              >
                                                {t('page.sceneNo', { n: idx + 1 })}
                                              </Tag>
                                              <span style={{
                                                fontWeight: 600,
                                                color: token.colorText,
                                                fontSize: isMobile ? 12 : 13,
                                                flex: 1,
                                                overflow: 'hidden',
                                                textOverflow: 'ellipsis',
                                                whiteSpace: 'nowrap'
                                              }}>
                                                📍 {scene.location}
                                              </span>
                                            </div>
                                            {scene.characters && scene.characters.length > 0 && (
                                              <div style={{
                                                fontSize: isMobile ? 10 : 11,
                                                color: token.colorTextSecondary,
                                                marginBottom: 4,
                                                paddingLeft: isMobile ? 2 : 4,
                                                overflow: 'hidden',
                                                textOverflow: 'ellipsis',
                                                whiteSpace: 'nowrap'
                                              }}>
                                                <span style={{ fontWeight: 500 }}>{t('page.sceneCharsPrefix')}</span>
                                                {scene.characters.join(' · ')}
                                              </div>
                                            )}
                                            {scene.purpose && (
                                              <div style={{
                                                fontSize: isMobile ? 10 : 11,
                                                color: token.colorTextSecondary,
                                                paddingLeft: isMobile ? 2 : 4,
                                                lineHeight: '1.5',
                                                overflow: 'hidden',
                                                textOverflow: 'ellipsis',
                                                whiteSpace: 'nowrap'
                                              }}>
                                                <span style={{ fontWeight: 500 }}>{t('page.scenePurposePrefix')}</span>
                                                {scene.purpose}
                                              </div>
                                            )}
                                          </div>
                                        );
                                      }
                                      })}
                                    </div>
                                  </div>
                                );
                              })() : null}
                            
                            {/* ✨ 关键事件展示 */}
                            {structureData.key_events && structureData.key_events.length > 0 && (
                              <div style={{
                                marginTop: 12,
                                padding: '10px 12px',
                                background: token.colorWarningBg,
                                borderLeft: `3px solid ${token.colorWarning}`,
                                borderRadius: token.borderRadius
                              }}>
                                <div style={{
                                  display: 'flex',
                                  alignItems: 'center',
                                  gap: 8,
                                  marginBottom: 8
                                }}>
                                  <span style={{
                                    fontSize: 13,
                                    fontWeight: 600,
                                    color: token.colorWarning,
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: 4
                                  }}>
                                    {t('page.keyEventsLabel')}
                                    <Tag
                                      color="orange"
                                      style={{
                                        margin: 0,
                                        fontSize: 11,
                                        borderRadius: 10,
                                        padding: '0 6px'
                                      }}
                                    >
                                      {structureData.key_events.length}
                                    </Tag>
                                  </span>
                                </div>
                                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                                  {structureData.key_events.map((event, idx) => (
                                    <div
                                      key={idx}
                                      style={{
                                        padding: '6px 10px',
                                        background: token.colorBgContainer,
                                        border: `1px solid ${token.colorWarningBorder}`,
                                        borderRadius: token.borderRadiusSM,
                                        fontSize: 12,
                                        color: token.colorWarningText,
                                        display: 'flex',
                                        alignItems: 'flex-start',
                                        gap: 8
                                      }}
                                    >
                                      <Tag
                                        color="orange"
                                        style={{
                                          margin: 0,
                                          fontSize: 11,
                                          borderRadius: 4,
                                          flexShrink: 0
                                        }}
                                      >
                                        {idx + 1}
                                      </Tag>
                                      <span style={{
                                        flex: 1,
                                        lineHeight: '1.6',
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap'
                                      }}>{event}</span>
                                    </div>
                                  ))}
                                </Space>
                              </div>
                            )}
                            
                            {/* ✨ 情节要点展示 (key_points) */}
                            {structureData.key_points && structureData.key_points.length > 0 && (
                              <div style={{
                                marginTop: 12,
                                padding: '10px 12px',
                                background: token.colorSuccessBg,
                                borderLeft: `3px solid ${token.colorSuccess}`,
                                borderRadius: token.borderRadius
                              }}>
                                <div style={{
                                  display: 'flex',
                                  alignItems: 'center',
                                  gap: 8,
                                  marginBottom: 8
                                }}>
                                  <span style={{
                                    fontSize: 13,
                                    fontWeight: 600,
                                    color: token.colorSuccess,
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: 4
                                  }}>
                                    {t('page.keyPointsLabel')}
                                    <Tag
                                      color="green"
                                      style={{
                                        margin: 0,
                                        fontSize: 11,
                                        borderRadius: 10,
                                        padding: '0 6px'
                                      }}
                                    >
                                      {structureData.key_points.length}
                                    </Tag>
                                  </span>
                                </div>
                                {/* 使用grid布局，移动端一列，桌面端两列 */}
                                <div style={{
                                  display: 'grid',
                                  gridTemplateColumns: isMobile ? '1fr' : 'repeat(auto-fill, minmax(280px, 1fr))',
                                  gap: isMobile ? 6 : 8,
                                  width: '100%',
                                  minWidth: 0
                                }}>
                                  {structureData.key_points.map((point, idx) => (
                                    <div
                                      key={idx}
                                      style={{
                                        padding: isMobile ? '6px 8px' : '8px 10px',
                                        background: token.colorBgContainer,
                                        border: `1px solid ${token.colorSuccessBorder}`,
                                        borderRadius: token.borderRadius,
                                        fontSize: isMobile ? 11 : 12,
                                        color: token.colorText,
                                        display: 'flex',
                                        alignItems: 'flex-start',
                                        gap: isMobile ? 6 : 8,
                                        transition: 'all 0.2s ease',
                                        cursor: 'default',
                                        width: '100%',
                                        minWidth: 0,
                                        boxSizing: 'border-box'
                                      }}
                                      onMouseEnter={(e) => {
                                        if (!isMobile) {
                                          e.currentTarget.style.borderColor = token.colorSuccess;
                                          e.currentTarget.style.boxShadow = `0 2px 8px ${alphaColor(token.colorSuccess, 0.25)}`;
                                        }
                                      }}
                                      onMouseLeave={(e) => {
                                        if (!isMobile) {
                                          e.currentTarget.style.borderColor = token.colorSuccessBorder;
                                          e.currentTarget.style.boxShadow = 'none';
                                        }
                                      }}
                                    >
                                      <Tag
                                        color="green"
                                        style={{
                                          margin: 0,
                                          fontSize: 10,
                                          borderRadius: 4,
                                          flexShrink: 0
                                        }}
                                      >
                                        {idx + 1}
                                      </Tag>
                                      <span style={{
                                        flex: 1,
                                        lineHeight: '1.6',
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap'
                                      }}>{point}</span>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                            
                            {/* ✨ 情感基调展示 (emotion) */}
                            {structureData.emotion && (
                              <div style={{
                                marginTop: 12,
                                padding: '10px 12px',
                                background: token.colorWarningBg,
                                borderLeft: `3px solid ${token.colorWarning}`,
                                borderRadius: token.borderRadius,
                                display: 'flex',
                                alignItems: 'center',
                                gap: 8
                              }}>
                                <span style={{
                                  fontSize: 13,
                                  fontWeight: 600,
                                  color: token.colorWarning
                                }}>
                                  {t('page.emotionPrefix')}
                                </span>
                                <Tag
                                  color="gold"
                                  style={{
                                    margin: 0,
                                    fontSize: 12,
                                    padding: '2px 12px',
                                    borderRadius: 12,
                                    background: token.colorBgContainer,
                                    border: `1px solid ${token.colorWarningBorder}`,
                                    color: token.colorWarningText
                                  }}
                                >
                                  {structureData.emotion}
                                </Tag>
                              </div>
                            )}
                            
                            {/* ✨ 叙事目标展示 (goal) */}
                            {structureData.goal && (
                              <div style={{
                                marginTop: 12,
                                padding: '10px 12px',
                                background: token.colorInfoBg,
                                borderLeft: `3px solid ${token.colorInfo}`,
                                borderRadius: token.borderRadius
                              }}>
                                <div style={{
                                  fontSize: 13,
                                  fontWeight: 600,
                                  color: token.colorInfo,
                                  marginBottom: 6
                                }}>
                                  {t('page.goalLabel')}
                                </div>
                                <div style={{
                                  fontSize: 12,
                                  color: token.colorText,
                                  lineHeight: '1.6',
                                  padding: '6px 10px',
                                  background: token.colorBgContainer,
                                  border: `1px solid ${token.colorInfoBorder}`,
                                  borderRadius: token.borderRadiusSM,
                                  overflow: 'hidden',
                                  textOverflow: 'ellipsis',
                                  whiteSpace: 'nowrap'
                                }}>
                                  {structureData.goal}
                                </div>
                              </div>
                            )}
                              </>
                            )}
                          </div>
                        }
                      />
                        
                        {/* 操作按钮区域 - 在卡片内部 */}
                        <div style={{
                          marginTop: 16,
                          paddingTop: 12,
                          borderTop: `1px solid ${token.colorBorderSecondary}`,
                          display: 'flex',
                          justifyContent: 'flex-end',
                          gap: 8
                        }}>
                          {currentProject?.outline_mode === 'one-to-many' && (
                            <Button
                              icon={<BranchesOutlined />}
                              onClick={() => handleExpandOutline(item.id, item.title)}
                              loading={isExpanding}
                              size={isMobile ? 'middle' : 'small'}
                            >
                              {t('listItem.expand')}
                            </Button>
                          )}
                          <Button
                            icon={<EditOutlined />}
                            onClick={() => handleOpenEditModal(item.id)}
                            size={isMobile ? 'middle' : 'small'}
                          >
                            {t('listItem.edit')}
                          </Button>
                          <Popconfirm
                            title={t('listItem.deleteConfirm')}
                            onConfirm={() => handleDeleteOutline(item.id)}
                            okText={t('listItem.deleteOk')}
                            cancelText={t('listItem.deleteCancel')}
                          >
                            <Button
                              danger
                              icon={<DeleteOutlined />}
                              size={isMobile ? 'middle' : 'small'}
                            >
                              {t('listItem.delete')}
                            </Button>
                          </Popconfirm>
                        </div>
                      </Card>
                    </List.Item>
                  );
                }}
              />
          )}

        </div>

        {/* 固定底部分页栏 */}
        {outlines.length > 0 && (
          <div
            style={{
              position: 'sticky',
              bottom: 0,
              zIndex: 10,
              backgroundColor: token.colorBgContainer,
              borderTop: `1px solid ${token.colorBorderSecondary}`,
              padding: isMobile ? '8px 0' : '10px 0',
              display: 'flex',
              justifyContent: 'flex-end'
            }}
          >
            <Pagination
              current={outlinePage}
              pageSize={outlinePageSize}
              total={filteredOutlines.length}
              showSizeChanger
              pageSizeOptions={['10', '20', '50', '100']}
              onChange={(page, size) => {
                setOutlinePage(page);
                if (size !== outlinePageSize) {
                  setOutlinePageSize(size);
                  setOutlinePage(1);
                }
              }}
              showTotal={(total) => t('pagination.total', { total })}
              size={isMobile ? 'small' : 'default'}
            />
          </div>
        )}
      </div>
    </>
  );
}
