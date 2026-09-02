import { useState, useEffect } from 'react';
import { Card, Form, Input, Button, Select, Slider, InputNumber, message, Space, Typography, Spin, Modal, Alert, Grid, Tabs, List, Tag, Popconfirm, Empty, Row, Col, Switch, theme } from 'antd';
import { SaveOutlined, DeleteOutlined, ReloadOutlined, InfoCircleOutlined, CheckCircleOutlined, CloseCircleOutlined, ThunderboltOutlined, PlusOutlined, EditOutlined, CopyOutlined, WarningOutlined, PictureOutlined } from '@ant-design/icons';
import { settingsApi, mcpPluginApi } from '../services/api';
import type { SettingsUpdate, APIKeyPreset, PresetCreateRequest, APIKeyPresetConfig } from '../types';
import { eventBus, EventNames } from '../store/eventBus';
import i18n, { normalizeLanguage } from '../i18n';
import { parseServerLanguage } from '../utils/languageSync';
import { Trans, useTranslation } from 'react-i18next';

const { Title, Text } = Typography;
const { Option } = Select;
const { useBreakpoint } = Grid;
const { TextArea } = Input;

export default function SettingsPage() {
  const { t } = useTranslation('settings');
  const { token } = theme.useToken();
  const screens = useBreakpoint();
  const isMobile = !screens.md; // md断点是768px
  const [form] = Form.useForm();
  const [modal, contextHolder] = Modal.useModal();
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [hasSettings, setHasSettings] = useState(false);
  const [isDefaultSettings, setIsDefaultSettings] = useState(false);
  const [modelOptions, setModelOptions] = useState<Array<{ value: string; label: string; description: string }>>([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [modelsFetched, setModelsFetched] = useState(false);
  const [modelSearchText, setModelSearchText] = useState('');
  const [testingApi, setTestingApi] = useState(false);
  const [testResult, setTestResult] = useState<{
    success: boolean;
    message: string;
    response_time_ms?: number;
    response_preview?: string;
    error?: string;
    error_type?: string;
    suggestions?: string[];
  } | null>(null);
  const [showTestResult, setShowTestResult] = useState(false);
  const [testingCoverApi, setTestingCoverApi] = useState(false);
  const [coverTestResult, setCoverTestResult] = useState<{
    success: boolean;
    message: string;
    provider?: string;
    model?: string;
  } | null>(null);

  // 预设相关状态
  const [activeTab, setActiveTab] = useState('current');
  const [uiLanguage, setUiLanguage] = useState<'zh' | 'en'>(() => (normalizeLanguage(i18n.language) === 'en' ? 'en' : 'zh'));
  const [savingLanguage, setSavingLanguage] = useState(false);
  const [presets, setPresets] = useState<APIKeyPreset[]>([]);
  const [presetsLoading, setPresetsLoading] = useState(false);
  const [activePresetId, setActivePresetId] = useState<string | undefined>();
  const [chapterAnalysisPresetId, setChapterAnalysisPresetId] = useState<string | undefined>();
  const [savingChapterAnalysisPreset, setSavingChapterAnalysisPreset] = useState(false);
  const [editingPreset, setEditingPreset] = useState<APIKeyPreset | null>(null);
  const [isPresetModalVisible, setIsPresetModalVisible] = useState(false);
  const [testingPresetId, setTestingPresetId] = useState<string | null>(null);
  const [presetForm] = Form.useForm();
  
  // 预设编辑窗口的模型列表状态（独立于当前配置的模型列表）
  const [presetModelOptions, setPresetModelOptions] = useState<Array<{ value: string; label: string; description: string }>>([]);
  const [fetchingPresetModels, setFetchingPresetModels] = useState(false);
  const [presetModelsFetched, setPresetModelsFetched] = useState(false);
  const [presetModelSearchText, setPresetModelSearchText] = useState('');

  const pageBackground = `linear-gradient(180deg, ${token.colorBgLayout} 0%, ${token.colorFillSecondary} 100%)`;
  const headerBackground = `linear-gradient(135deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`;

  useEffect(() => {
    loadSettings();
    if (activeTab === 'presets') {
      loadPresets();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (activeTab === 'presets') {
      loadPresets();
    } else if (activeTab === 'current') {
      // 切换到当前配置Tab时，刷新设置以获取最新数据
      loadSettings();
      // 清除旧的测试结果，因为可能是其他配置的测试结果
      setTestResult(null);
      setShowTestResult(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  const loadSettings = async () => {
    setInitialLoading(true);
    try {
      const settings = await settingsApi.getSettings();

      const serverLang = parseServerLanguage(settings.preferences);
      if (serverLang && normalizeLanguage(i18n.language) !== serverLang) {
        await i18n.changeLanguage(serverLang);
      }
      if (serverLang) {
        setUiLanguage(serverLang);
      }

      form.setFieldsValue({
        ...defaultCoverSettings,
        ...settings,
        cover_api_provider: settings.cover_api_provider || defaultCoverSettings.cover_api_provider,
        cover_api_key: settings.cover_api_key ?? defaultCoverSettings.cover_api_key,
        cover_api_base_url: settings.cover_api_base_url || defaultCoverSettings.cover_api_base_url,
        cover_image_model: settings.cover_image_model || defaultCoverSettings.cover_image_model,
        cover_enabled: settings.cover_enabled ?? defaultCoverSettings.cover_enabled,
      });

      // 判断是否为默认设置（id='0'表示来自.env的默认配置）
      if (settings.id === '0' || !settings.id) {
        setIsDefaultSettings(true);
        setHasSettings(false);
      } else {
        setIsDefaultSettings(false);
        setHasSettings(true);
      }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
      // 如果404表示还没有设置，使用默认值
      if (error?.response?.status === 404) {
        setHasSettings(false);
        setIsDefaultSettings(true);
        form.setFieldsValue({
          api_provider: 'openai',
          api_base_url: 'https://api.openai.com/v1',
          llm_model: 'gpt-4',
          temperature: 0.7,
          max_tokens: 2000,
          disable_thinking: false,
          ...defaultCoverSettings,
        });
      } else {
        message.error(t('toast.loadFailed'));
      }
    } finally {
      setInitialLoading(false);
    }
  };

  const handleLanguageChange = async (lang: 'zh' | 'en') => {
    setUiLanguage(lang);
    setSavingLanguage(true);
    try {
      await i18n.changeLanguage(lang);
      await settingsApi.updatePreferences({ language: lang });
      message.success(t('language.updated'));
    } catch (error) {
      console.error('save ui language preference failed:', error);
      message.warning(t('language.syncFailed'));
    } finally {
      setSavingLanguage(false);
    }
  };

  const handleSave = async (values: SettingsUpdate) => {
    setLoading(true);
    try {
      const normalizedValues: SettingsUpdate = {
        ...values,
        api_key: builtInKeyProviders.includes(values.api_provider || '') ? '' : values.api_key,
      };
      // 检查是否与 MCP 缓存的配置不一致
      const verifiedConfigStr = localStorage.getItem('mcp_verified_config');
      let configChanged = false;
      
      if (verifiedConfigStr) {
        try {
          const verifiedConfig = JSON.parse(verifiedConfigStr);
          configChanged =
            verifiedConfig.provider !== normalizedValues.api_provider ||
            verifiedConfig.baseUrl !== normalizedValues.api_base_url ||
            verifiedConfig.model !== normalizedValues.llm_model;
        } catch (e) {
          console.error('Failed to parse verified config:', e);
        }
      }
      
      await settingsApi.saveSettings(normalizedValues);
      message.success(t('toast.saved'));
      setHasSettings(true);
      setIsDefaultSettings(false);
      
      // 保存后清除测试结果，因为配置可能已变更
      setTestResult(null);
      setShowTestResult(false);
      
      // 手动保存配置后，同步刷新预设激活状态。
      // 后端会在配置与激活预设不一致时自动取消激活，这里统一拉取最新状态，
      // 确保设置界面与预设列表联动一致。
      const previousActivePresetId = activePresetId;
      await loadPresets();
      
      if (previousActivePresetId) {
        const latestPresets = await settingsApi.getPresets();
        const stillActive = latestPresets.active_preset_id === previousActivePresetId;
        if (!stillActive) {
          setActivePresetId(undefined);
          message.info(t('toast.presetDeactivated'));
        }
      }
      
      // 如果配置发生变化，需要处理 MCP 插件
      if (configChanged) {
        // 清除 MCP 验证缓存
        localStorage.removeItem('mcp_verified_config');
        
        // 检查并禁用所有 MCP 插件
        try {
          const plugins = await mcpPluginApi.getPlugins();
          const activePlugins = plugins.filter(p => p.enabled);
          
          if (activePlugins.length > 0) {
            // 禁用所有插件
            message.loading({ content: t('toast.disablingMcp'), key: 'disable_mcp' });
            await Promise.all(activePlugins.map(p => mcpPluginApi.togglePlugin(p.id, false)));
            message.success({ content: t('toast.mcpDisabled'), key: 'disable_mcp' });
            
            // 显示提示弹窗
            modal.warning({
              title: (
                <Space>
                  <WarningOutlined style={{ color: token.colorWarning }} />
                  <span>{t('mcp.configChangedTitle')}</span>
                </Space>
              ),
              centered: true,
              content: (
                <div style={{ padding: '8px 0' }}>
                  <Alert
                    message={t('mcp.configChangedBody')}
                    type="warning"
                    showIcon
                    style={{ marginBottom: 16 }}
                  />
                  <div style={{
                    padding: 12,
                    background: token.colorInfoBg,
                    border: `1px solid ${token.colorInfoBorder}`,
                    borderRadius: 8
                  }}>
                    <Text strong style={{ display: 'block', marginBottom: 8 }}>{t('mcp.stepsLabel')}</Text>
                    <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13 }}>
                      <li>{t('mcp.step1')}</li>
                      <li>{t('mcp.step2')}</li>
                      <li>{t('mcp.step3')}</li>
                    </ol>
                  </div>
                </div>
              ),
              okText: t('mcp.goToMcp'),
              cancelText: t('mcp.later'),
              onOk: () => {
                eventBus.emit(EventNames.SWITCH_TO_MCP_VIEW);
              },
            });
          }
        } catch (err) {
          console.error('Failed to disable MCP plugins:', err);
        }
      }
    } catch {
      message.error(t('toast.saveFailed'));
    } finally {
      setLoading(false);
    }
  };

  const handleReset = () => {
    modal.confirm({
      title: t('confirm.resetTitle'),
      content: t('confirm.resetContent'),
      centered: true,
      okText: t('confirm.ok'),
      cancelText: t('confirm.cancel'),
      onOk: () => {
        form.setFieldsValue({
          api_provider: 'openai',
          api_key: '',
          api_base_url: 'https://api.openai.com/v1',
          llm_model: 'gpt-4',
          temperature: 0.7,
          max_tokens: 2000,
          disable_thinking: false,
          ...defaultCoverSettings,
        });
        message.info(t('toast.resetDone'));
      },
    });
  };

  const handleDelete = () => {
    modal.confirm({
      title: t('confirm.deleteTitle'),
      content: t('confirm.deleteContent'),
      centered: true,
      okText: t('confirm.ok'),
      cancelText: t('confirm.cancel'),
      okType: 'danger',
      onOk: async () => {
        setLoading(true);
        try {
          await settingsApi.deleteSettings();
          message.success(t('toast.deleted'));
          setHasSettings(false);
          form.resetFields();
        } catch {
          message.error(t('toast.deleteFailed'));
        } finally {
          setLoading(false);
        }
      },
    });
  };

  const xiaomiMimoDefaultUrl = 'https://token-plan-cn.xiaomimimo.com/v1';
  const builtInKeyProviders = ['xiaomi_mimo'];
  const xiaomiMimoDefaultModels = [
    { value: 'mimo-v2.5', label: 'mimo-v2.5', description: t('provider.xiaomiMimoDefaultModelDesc') },
  ];
  const defaultCoverSettings = {
    cover_enabled: false,
    cover_api_provider: 'gemini',
    cover_api_key: '',
    cover_api_base_url: 'https://generativelanguage.googleapis.com/v1beta',
    cover_image_model: '',
  };

  const apiProviders = [
    {
      value: 'xiaomi_mimo',
      label: t('provider.xiaomiMimoBuiltIn'),
      defaultUrl: xiaomiMimoDefaultUrl,
      defaultModel: xiaomiMimoDefaultModels[0].value,
      builtInKey: true,
    },
    { value: 'openai', label: 'OpenAI Compatible', defaultUrl: 'https://api.openai.com/v1' },
    // { value: 'anthropic', label: 'Anthropic (Claude)', defaultUrl: 'https://api.anthropic.com' },
    { value: 'gemini', label: 'Google Gemini', defaultUrl: 'https://generativelanguage.googleapis.com/v1beta' },
    { value: 'commandcode', label: 'Command Code GOAT', defaultUrl: 'https://api.commandcode.ai/provider/v1' },
  ];

  const selectedProvider = Form.useWatch('api_provider', form);
  const selectedCoverProvider = Form.useWatch('cover_api_provider', form);
  const selectedPresetProvider = Form.useWatch('api_provider', presetForm);

  const handleProviderChange = (value: string) => {
    const provider = apiProviders.find(p => p.value === value);
    if (provider) {
      const nextValues: Record<string, string> = {};
      if (provider.defaultUrl) {
        nextValues.api_base_url = provider.defaultUrl;
      }
      if (builtInKeyProviders.includes(provider.value)) {
        nextValues.api_key = '';
        nextValues.llm_model = provider.defaultModel || xiaomiMimoDefaultModels[0].value;
      }
      if (provider.value === 'commandcode') {
        nextValues.llm_model = 'deepseek/deepseek-v4-flash';
      }
      form.setFieldsValue(nextValues);
    }
    // 清空模型列表，需要重新获取
    setModelOptions([]);
    setModelsFetched(false);
  };

  const coverApiProviders = [
    { value: 'gemini', label: 'Google Gemini', defaultUrl: 'https://generativelanguage.googleapis.com/v1beta' },
    { value: 'grok', label: 'Grok', defaultUrl: 'https://api.x.ai/v1' },
  ];

  const handleCoverProviderChange = (value: string) => {
    const provider = coverApiProviders.find(p => p.value === value);
    if (!provider) {
      setCoverTestResult(null);
      return;
    }

    const nextValues: Record<string, string> = {};
    if (provider.defaultUrl) {
      nextValues.cover_api_base_url = provider.defaultUrl;
    }

    form.setFieldsValue(nextValues);
    setCoverTestResult(null);
  };

  const handleCoverTestConnection = async () => {
    const coverApiProvider = form.getFieldValue('cover_api_provider');
    const coverApiKey = form.getFieldValue('cover_api_key');
    const coverApiBaseUrl = form.getFieldValue('cover_api_base_url');
    const coverImageModel = form.getFieldValue('cover_image_model');

    if (!coverApiProvider || !coverApiKey || !coverImageModel) {
      message.warning(t('toast.coverConfigIncomplete'));
      return;
    }

    setTestingCoverApi(true);
    setCoverTestResult(null);
    try {
      const result = await settingsApi.testCoverConnection({
        cover_api_provider: coverApiProvider,
        cover_api_key: coverApiKey,
        cover_api_base_url: coverApiBaseUrl,
        cover_image_model: coverImageModel,
      });
      setCoverTestResult(result);
      if (result.success) {
        message.success(t('toast.coverTestSuccess'));
      } else {
        message.error(result.message || t('toast.coverTestFailed'));
      }
    } catch (error) {
      console.error('cover api test failed:', error);
      setCoverTestResult({
        success: false,
        message: t('toast.coverTestFailed'),
      });
    } finally {
      setTestingCoverApi(false);
    }
  };

  const handleFetchModels = async (silent: boolean = false) => {
    const apiKey = form.getFieldValue('api_key');
    const apiBaseUrl = form.getFieldValue('api_base_url');
    const provider = form.getFieldValue('api_provider');

    const isBuiltInKeyProvider = builtInKeyProviders.includes(provider);

    if ((!apiKey && !isBuiltInKeyProvider) || !apiBaseUrl) {
      if (!silent) {
        message.warning(t('toast.fillKeyAndUrl'));
      }
      return;
    }

    setFetchingModels(true);
    try {
      const response = await settingsApi.getAvailableModels({
        api_key: isBuiltInKeyProvider ? '' : apiKey,
        api_base_url: apiBaseUrl,
        provider: provider || 'openai'
      });

      setModelOptions(response.models);
      setModelsFetched(true);
      if (!silent) {
        message.success(t('toast.modelsFetched', { n: response.count || response.models.length }));
      }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
      const errorMsg = error?.response?.data?.detail || t('toast.modelsFetchFailed');
      if (!silent) {
        message.error(errorMsg);
      }
      setModelOptions([]);
      setModelsFetched(true); // 即使失败也标记为已尝试，避免重复请求
    } finally {
      setFetchingModels(false);
    }
  };

  const handleModelSelectFocus = () => {
    // 如果还没有获取过模型列表，自动获取
    if (!modelsFetched && !fetchingModels) {
      handleFetchModels(true); // silent模式，不显示成功消息
    }
  };

  const handleTestConnection = async () => {
    const apiKey = form.getFieldValue('api_key');
    const apiBaseUrl = form.getFieldValue('api_base_url');
    const provider = form.getFieldValue('api_provider');
    const modelName = form.getFieldValue('llm_model');
    const temperature = form.getFieldValue('temperature');
    const maxTokens = form.getFieldValue('max_tokens');

    const isBuiltInKeyProvider = builtInKeyProviders.includes(provider);

    if ((!apiKey && !isBuiltInKeyProvider) || !apiBaseUrl || !provider || !modelName) {
      message.warning(t('toast.configIncomplete'));
      return;
    }

    setTestingApi(true);
    setTestResult(null);

    try {
      const result = await settingsApi.testApiConnection({
        api_key: isBuiltInKeyProvider ? '' : apiKey,
        api_base_url: apiBaseUrl,
        provider: provider,
        llm_model: modelName,
        temperature: temperature,
        max_tokens: maxTokens
      });

      setTestResult(result);
      setShowTestResult(true);

      if (result.success) {
        message.success(t('toast.testSuccess', { ms: result.response_time_ms ?? 0 }));
      } else {
        message.error(t('toast.testFailedSeeDetails'));
      }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
      const errorMsg = error?.response?.data?.detail || t('toast.testRequestFailed');
      message.error(errorMsg);
      setTestResult({
        success: false,
        message: t('toast.testRequestFailed'),
        error: errorMsg,
        error_type: 'RequestError',
        suggestions: [t('toast.checkNetwork'), t('toast.checkBackend')]
      });
      setShowTestResult(true);
    } finally {
      setTestingApi(false);
    }
  };

  // ========== 预设管理函数 ==========

  const loadPresets = async () => {
    setPresetsLoading(true);
    try {
      const response = await settingsApi.getPresets();
      setPresets(response.presets);
      setActivePresetId(response.active_preset_id);
      setChapterAnalysisPresetId(response.chapter_analysis_preset_id);
    } catch (error) {
      message.error(t('toast.presetsLoadFailed'));
      console.error(error);
    } finally {
      setPresetsLoading(false);
    }
  };

  const showPresetModal = (preset?: APIKeyPreset) => {
    // 重置预设模型列表状态
    setPresetModelOptions([]);
    setPresetModelsFetched(false);
    
    if (preset) {
      setEditingPreset(preset);
      presetForm.setFieldsValue({
        name: preset.name,
        description: preset.description,
        ...preset.config,
      });
    } else {
      setEditingPreset(null);
      presetForm.resetFields();
      presetForm.setFieldsValue({
        api_provider: 'openai',
        api_base_url: 'https://api.openai.com/v1',
        temperature: 0.7,
        max_tokens: 32000,
      });
    }
    setIsPresetModalVisible(true);
  };

  const handlePresetCancel = () => {
    setIsPresetModalVisible(false);
    setEditingPreset(null);
    presetForm.resetFields();
    // 清除预设模型列表状态
    setPresetModelOptions([]);
    setPresetModelsFetched(false);
    setPresetModelSearchText('');
  };

  // 预设编辑窗口：获取模型列表
  const handleFetchPresetModels = async (silent: boolean = false) => {
    const apiKey = presetForm.getFieldValue('api_key');
    const apiBaseUrl = presetForm.getFieldValue('api_base_url');
    const provider = presetForm.getFieldValue('api_provider');

    const isBuiltInKeyProvider = builtInKeyProviders.includes(provider);

    if ((!apiKey && !isBuiltInKeyProvider) || !apiBaseUrl) {
      if (!silent) {
        message.warning(t('toast.fillKeyAndUrl'));
      }
      return;
    }

    setFetchingPresetModels(true);
    try {
      const response = await settingsApi.getAvailableModels({
        api_key: isBuiltInKeyProvider ? '' : apiKey,
        api_base_url: apiBaseUrl,
        provider: provider || 'openai'
      });

      setPresetModelOptions(response.models);
      setPresetModelsFetched(true);
      if (!silent) {
        message.success(t('toast.modelsFetched', { n: response.count || response.models.length }));
      }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
      const errorMsg = error?.response?.data?.detail || t('toast.modelsFetchFailed');
      if (!silent) {
        message.error(errorMsg);
      }
      setPresetModelOptions([]);
      setPresetModelsFetched(true);
    } finally {
      setFetchingPresetModels(false);
    }
  };

  // 预设编辑窗口：模型选择框获得焦点时自动获取
  const handlePresetModelSelectFocus = () => {
    if (!presetModelsFetched && !fetchingPresetModels) {
      handleFetchPresetModels(true);
    }
  };

  // 预设编辑窗口：提供商变更时更新默认URL并清空模型列表
  const handlePresetProviderChange = (value: string) => {
    const provider = apiProviders.find(p => p.value === value);
    if (provider) {
      const nextValues: Record<string, string> = {};
      if (provider.defaultUrl) {
        nextValues.api_base_url = provider.defaultUrl;
      }
      if (builtInKeyProviders.includes(provider.value)) {
        nextValues.api_key = '';
        nextValues.llm_model = provider.defaultModel || xiaomiMimoDefaultModels[0].value;
      }
      presetForm.setFieldsValue(nextValues);
    }
    // 清空模型列表，需要重新获取
    setPresetModelOptions([]);
    setPresetModelsFetched(false);
  };

  const handlePresetSave = async () => {
    try {
      const values = await presetForm.validateFields();
      const isBuiltInKeyProvider = builtInKeyProviders.includes(values.api_provider);
      const config: APIKeyPresetConfig = {
        api_provider: values.api_provider,
        api_key: isBuiltInKeyProvider ? '' : values.api_key,
        api_base_url: values.api_base_url,
        llm_model: values.llm_model,
        temperature: values.temperature,
        max_tokens: values.max_tokens,
        system_prompt: values.system_prompt,
      };

      if (editingPreset) {
        await settingsApi.updatePreset(editingPreset.id, {
          name: values.name,
          description: values.description,
          config,
        });
        message.success(t('toast.presetUpdated'));
      } else {
        const request: PresetCreateRequest = {
          name: values.name,
          description: values.description,
          config,
        };
        await settingsApi.createPreset(request);
        message.success(t('toast.presetCreated'));
      }

      handlePresetCancel();
      loadPresets();
    } catch (error) {
      console.error('save preset failed:', error);
    }
  };

  const handleChapterAnalysisPresetChange = async (presetId?: string) => {
    setSavingChapterAnalysisPreset(true);
    try {
      const normalizedPresetId = presetId || undefined;
      await settingsApi.setChapterAnalysisPresetSelection(normalizedPresetId);
      setChapterAnalysisPresetId(normalizedPresetId);
      message.success(normalizedPresetId ? t('toast.chapterAnalysisPresetSet') : t('toast.chapterAnalysisPresetReset'));
      loadPresets();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
      message.error(error.response?.data?.detail || t('toast.chapterAnalysisPresetFailed'));
      console.error(error);
    } finally {
      setSavingChapterAnalysisPreset(false);
    }
  };

  const handlePresetDelete = async (presetId: string) => {
    try {
      await settingsApi.deletePreset(presetId);
      message.success(t('toast.presetDeleted'));
      loadPresets();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
      message.error(error.response?.data?.detail || t('toast.deleteFailedShort'));
      console.error(error);
    }
  };

  const handlePresetActivate = async (presetId: string, presetName: string) => {
    try {
      // 获取预设配置用于比较
      const preset = presets.find(p => p.id === presetId);
      
      await settingsApi.activatePreset(presetId);
      message.success(t('toast.presetActivated', { name: presetName }));
      
      // 激活预设后清除当前配置Tab的测试结果
      setTestResult(null);
      setShowTestResult(false);
      
      // 清除模型列表缓存，因为API配置可能已变更
      setModelOptions([]);
      setModelsFetched(false);
      
      loadPresets();
      loadSettings(); // 重新加载当前配置
      
      // 检查是否与 MCP 缓存的配置不一致
      if (preset) {
        const verifiedConfigStr = localStorage.getItem('mcp_verified_config');
        let configChanged = false;
        
        if (verifiedConfigStr) {
          try {
            const verifiedConfig = JSON.parse(verifiedConfigStr);
            configChanged =
              verifiedConfig.provider !== preset.config.api_provider ||
              verifiedConfig.baseUrl !== preset.config.api_base_url ||
              verifiedConfig.model !== preset.config.llm_model;
          } catch (e) {
            console.error('Failed to parse verified config:', e);
            configChanged = true; // 解析失败也视为配置变化
          }
        } else {
          // 没有缓存的配置，如果有启用的插件也需要处理
          configChanged = true;
        }
        
        if (configChanged) {
          // 清除 MCP 验证缓存
          localStorage.removeItem('mcp_verified_config');
          
          // 检查并禁用所有 MCP 插件
          try {
            const plugins = await mcpPluginApi.getPlugins();
            const activePlugins = plugins.filter(p => p.enabled);
            
            if (activePlugins.length > 0) {
              // 禁用所有插件
              message.loading({ content: t('toast.disablingMcp'), key: 'disable_mcp' });
              await Promise.all(activePlugins.map(p => mcpPluginApi.togglePlugin(p.id, false)));
              message.success({ content: t('toast.mcpDisabled'), key: 'disable_mcp' });
              
              // 显示提示弹窗
              modal.warning({
                title: (
                  <Space>
                    <WarningOutlined style={{ color: token.colorWarning }} />
                    <span>{t('mcp.configChangedTitle')}</span>
                  </Space>
                ),
                centered: true,
                content: (
                  <div style={{ padding: '8px 0' }}>
                    <Alert
                      message={t('mcp.configChangedPresetBody', { name: presetName })}
                      type="warning"
                      showIcon
                      style={{ marginBottom: 16 }}
                    />
                    <div style={{
                      padding: 12,
                      background: token.colorInfoBg,
                      border: `1px solid ${token.colorInfoBorder}`,
                      borderRadius: 8
                    }}>
                      <Text strong style={{ display: 'block', marginBottom: 8 }}>{t('mcp.stepsLabel')}</Text>
                      <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13 }}>
                        <li>{t('mcp.step1')}</li>
                        <li>{t('mcp.step2')}</li>
                        <li>{t('mcp.step3')}</li>
                      </ol>
                    </div>
                  </div>
                ),
                okText: t('mcp.goToMcp'),
                cancelText: t('mcp.later'),
                onOk: () => {
                  eventBus.emit(EventNames.SWITCH_TO_MCP_VIEW);
                },
              });
            }
          } catch (err) {
            console.error('Failed to disable MCP plugins:', err);
          }
        }
      }
    } catch (error) {
      message.error(t('toast.activateFailed'));
      console.error(error);
    }
  };

  const handlePresetTest = async (presetId: string) => {
    setTestingPresetId(presetId);
    try {
      const result = await settingsApi.testPreset(presetId);
      if (result.success) {
        modal.success({
          title: t('presets.testSuccessTitle'),
          centered: true,
          width: isMobile ? '90%' : 600,
          content: (
            <div style={{ padding: '8px 0' }}>
              <div style={{ marginBottom: 24, padding: 16, background: token.colorSuccessBg, border: `1px solid ${token.colorSuccessBorder}`, borderRadius: 8 }}>
                <Typography.Text strong style={{ color: token.colorSuccess }}>
                  {t('presets.connectionOk')}
                </Typography.Text>
              </div>

              <div style={{
                padding: 16,
                background: token.colorBgLayout,
                borderRadius: 8,
                marginBottom: 16
              }}>
                <div style={{ marginBottom: 8, fontSize: 14 }}>
                  <Text type="secondary">{t('presets.providerLabel')}</Text>
                  <Text strong>{result.provider?.toUpperCase() || 'N/A'}</Text>
                </div>
                <div style={{ marginBottom: 8, fontSize: 14 }}>
                  <Text type="secondary">{t('presets.modelLabel')}</Text>
                  <Text strong>{result.model || 'N/A'}</Text>
                </div>
                {result.response_time_ms !== undefined && (
                  <div style={{ fontSize: 14 }}>
                    <Text type="secondary">{t('presets.responseTimeLabel')}</Text>
                    <Text strong>{result.response_time_ms}ms</Text>
                  </div>
                )}
              </div>

              <Alert
                message={t('presets.testPassAlert')}
                type="success"
                showIcon
              />
            </div>
          ),
        });
      } else {
        modal.error({
          title: t('presets.testFailedTitle'),
          centered: true,
          width: isMobile ? '90%' : 600,
          content: (
            <div style={{ padding: '8px 0' }}>
              <div style={{ marginBottom: 16 }}>
                <Alert
                  message={result.message || t('presets.apiTestFailed')}
                  type="error"
                  showIcon
                />
              </div>

              {result.error && (
                <div style={{
                  padding: 16,
                  background: token.colorErrorBg,
                  border: `1px solid ${token.colorErrorBorder}`,
                  borderRadius: 8,
                  marginBottom: 16
                }}>
                  <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 8 }}>{t('presets.errorLabel')}</Text>
                  <Text style={{ fontSize: 13, color: token.colorError, fontFamily: 'monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {result.error}
                  </Text>
                </div>
              )}

              {result.suggestions && result.suggestions.length > 0 && (
                <div style={{
                  padding: 16,
                  background: token.colorWarningBg,
                  border: `1px solid ${token.colorWarningBorder}`,
                  borderRadius: 8,
                  marginBottom: 16
                }}>
                  <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 8 }}>{t('presets.suggestionsLabel')}</Text>
                  <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13 }}>
                    {result.suggestions.map((s, i) => (
                      <li key={i} style={{ marginBottom: 4 }}>{s}</li>
                    ))}
                  </ul>
                </div>
              )}

              <Alert
                message={t('presets.testProblemAlert')}
                type="warning"
                showIcon
              />
            </div>
          ),
        });
      }
    } catch (error) {
      message.error(t('toast.testFailed'));
      console.error(error);
    } finally {
      setTestingPresetId(null);
    }
  };

  const handleCreateFromCurrent = () => {
    const currentConfig = form.getFieldsValue();
    presetForm.setFieldsValue({
      name: '',
      description: '',
      ...currentConfig,
    });
    setEditingPreset(null);
    setIsPresetModalVisible(true);
  };

  const getProviderColor = (provider: string) => {
    switch (provider) {
      case 'openai':
        return 'blue';
      // case 'anthropic':
      //   return 'purple';
      case 'gemini':
        return 'green';
      default:
        return 'default';
    }
  };

  // ========== 渲染预设列表 ==========

  const renderPresetsList = () => (
    <Spin spinning={presetsLoading}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Text type="secondary">{t('presets.manageHint')}</Text>
          <Space>
            <Button icon={<CopyOutlined />} onClick={handleCreateFromCurrent}>
              {t('presets.createFromCurrent')}
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => showPresetModal()}>
              {t('presets.createNew')}
            </Button>
          </Space>
        </div>

        <Card size="small" style={{ background: token.colorFillAlter, borderColor: token.colorBorderSecondary }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap align="center" style={{ width: '100%', justifyContent: 'space-between' }}>
              <Space direction="vertical" size={2}>
                <Text strong>{t('presets.chapterAnalysisTitle')}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {t('presets.chapterAnalysisDesc')}
                </Text>
              </Space>
              <Select
                allowClear
                placeholder={t('presets.defaultOption')}
                value={chapterAnalysisPresetId}
                loading={savingChapterAnalysisPreset}
                disabled={presetsLoading || savingChapterAnalysisPreset}
                style={{ minWidth: isMobile ? '100%' : 280 }}
                onChange={(value) => handleChapterAnalysisPresetChange(value)}
                options={presets.map((preset) => ({
                  value: preset.id,
                  label: `${preset.name} (${preset.config.llm_model})`,
                }))}
              />
            </Space>
            <Alert
              showIcon
              type="info"
              message={chapterAnalysisPresetId ? t('presets.chapterAnalysisPreferred') : t('presets.chapterAnalysisDefault')}
              style={{ padding: '6px 10px' }}
            />
          </Space>
        </Card>

        {presets.length === 0 ? (
          <Empty
            description={t('presets.emptyText')}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            style={{ margin: '40px 0' }}
          >
            <Button type="primary" icon={<PlusOutlined />} onClick={() => showPresetModal()}>
              {t('presets.createFirst')}
            </Button>
          </Empty>
        ) : (
          <List
            dataSource={presets}
            renderItem={(preset) => {
              const isActive = preset.id === activePresetId;
              return (
                <List.Item
                  key={preset.id}
                  style={{
                    background: isActive ? token.colorInfoBg : 'transparent',
                    padding: '16px',
                    marginBottom: '8px',
                    border: isActive ? `2px solid ${token.colorPrimary}` : `1px solid ${token.colorBorderSecondary}`,
                    borderRadius: '8px',
                  }}
                  actions={[
                    !isActive && (
                      <Button
                        type="link"
                        onClick={() => handlePresetActivate(preset.id, preset.name)}
                      >
                        {t('presets.activate')}
                      </Button>
                    ),
                    <Button
                      key="test"
                      type="link"
                      icon={<ThunderboltOutlined />}
                      loading={testingPresetId === preset.id}
                      onClick={() => handlePresetTest(preset.id)}
                    >
                      {t('presets.test')}
                    </Button>,
                    <Button
                      type="link"
                      icon={<EditOutlined />}
                      onClick={() => showPresetModal(preset)}
                    >
                      {t('presets.edit')}
                    </Button>,
                    <Popconfirm
                      title={t('presets.deleteConfirm')}
                      onConfirm={() => handlePresetDelete(preset.id)}
                      disabled={isActive}
                      okText={t('confirm.ok')}
                      cancelText={t('confirm.cancel')}
                    >
                      <Button
                        type="link"
                        danger
                        icon={<DeleteOutlined />}
                        disabled={isActive}
                      >
                        {t('buttons.delete')}
                      </Button>
                    </Popconfirm>,
                  ].filter(Boolean)}
                >
                  <List.Item.Meta
                    avatar={
                      isActive && (
                        <CheckCircleOutlined
                          style={{ fontSize: '24px', color: token.colorSuccess }}
                        />
                      )
                    }
                    title={
                      <Space>
                        <span style={{ fontWeight: 'bold' }}>{preset.name}</span>
                        {isActive && <Tag color="success">{t('presets.activeTag')}</Tag>}
                        {preset.id === chapterAnalysisPresetId && <Tag color="processing">{t('presets.chapterAnalysisTag')}</Tag>}
                      </Space>
                    }
                    description={
                      <Space direction="vertical" size="small" style={{ width: '100%' }}>
                        {preset.description && (
                          <div style={{ color: token.colorTextSecondary }}>{preset.description}</div>
                        )}
                        <Space wrap>
                          <Tag color={getProviderColor(preset.config.api_provider)}>
                            {preset.config.api_provider.toUpperCase()}
                          </Tag>
                          <Tag>{preset.config.llm_model}</Tag>
                          <Tag>{t('presets.temperatureTag', { value: preset.config.temperature })}</Tag>
                          <Tag>Tokens: {preset.config.max_tokens}</Tag>
                        </Space>
                        <div style={{ fontSize: '12px', color: token.colorTextTertiary }}>
                          {t('presets.createdPrefix')} {new Date(preset.created_at).toLocaleString()}
                        </div>
                      </Space>
                    }
                  />
                </List.Item>
              );
            }}
          />
        )}
      </Space>
    </Spin>
  );

  return (
    <>
      {contextHolder}
      <div style={{
        minHeight: '90vh',
        background: pageBackground,
        padding: isMobile ? '20px 16px 70px' : '24px 24px 70px',
        display: 'flex',
        flexDirection: 'column',
      }}>
        <div style={{
          maxWidth: 1400,
          margin: '0 auto',
          width: '100%',
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
        }}>
          {/* 顶部导航卡片 */}
          <Card
            variant="borderless"
            style={{
              background: headerBackground,
              borderRadius: isMobile ? 16 : 24,
              boxShadow: token.boxShadowSecondary,
              marginBottom: isMobile ? 20 : 24,
              border: 'none',
              position: 'relative',
              overflow: 'hidden'
            }}
          >
            {/* 装饰性背景元素 */}
            <div style={{ position: 'absolute', top: -60, right: -60, width: 200, height: 200, borderRadius: '50%', background: token.colorWhite, opacity: 0.08, pointerEvents: 'none' }} />
            <div style={{ position: 'absolute', bottom: -40, left: '30%', width: 120, height: 120, borderRadius: '50%', background: token.colorWhite, opacity: 0.05, pointerEvents: 'none' }} />
            <div style={{ position: 'absolute', top: '50%', right: '15%', width: 80, height: 80, borderRadius: '50%', background: token.colorWhite, opacity: 0.06, pointerEvents: 'none' }} />

            <Row align="middle" justify="space-between" gutter={[16, 16]} style={{ position: 'relative', zIndex: 1 }}>
              <Col xs={24} sm={12}>
                <Space direction="vertical" size={4}>
                  <Title level={isMobile ? 3 : 2} style={{ margin: 0, color: token.colorWhite, textShadow: `0 2px 4px ${token.colorBgMask}` }}>
                    {t('title')}
                  </Title>
                  <Text style={{ fontSize: isMobile ? 12 : 14, color: token.colorTextLightSolid, marginLeft: isMobile ? 40 : 48, opacity: 0.85 }}>
                    {t('subtitle')}
                  </Text>
                </Space>
              </Col>
              <Col xs={24} sm={12}>
                {/* 按钮区域预留 */}
              </Col>
            </Row>
          </Card>

          <Card
            variant="borderless"
            style={{
              background: token.colorBgContainer,
              borderRadius: isMobile ? 12 : 16,
              boxShadow: token.boxShadowSecondary,
              marginBottom: isMobile ? 20 : 24,
            }}
          >
            <Row align="middle" justify="space-between" gutter={[16, 12]}>
              <Col xs={24} sm={12}>
                <Space direction="vertical" size={2}>
                  <Text strong>{t('language.label')}</Text>
                  <Text type="secondary" style={{ fontSize: isMobile ? 12 : 13 }}>
                    {t('language.description')}
                  </Text>
                </Space>
              </Col>
              <Col xs={24} sm={12} style={{ textAlign: isMobile ? 'left' : 'right' }}>
                <Select
                  value={uiLanguage}
                  onChange={handleLanguageChange}
                  loading={savingLanguage}
                  style={{ minWidth: 160 }}
                  options={[
                    { value: 'zh', label: t('language.zhLabel') },
                    { value: 'en', label: t('language.enLabel') },
                  ]}
                />
              </Col>
            </Row>
          </Card>

          {/* 主内容卡片 */}
          <Card
            variant="borderless"
            style={{
              background: token.colorBgContainer,
              borderRadius: isMobile ? 12 : 16,
              boxShadow: token.boxShadowSecondary,
              flex: 1,
            }}
            styles={{
              body: {
                padding: isMobile ? '16px' : '24px'
              }
            }}
          >
            <Tabs
              activeKey={activeTab}
              onChange={setActiveTab}
              items={[
                {
                  key: 'current',
                  label: <Space size={6}><ThunderboltOutlined />{t('tabs.current')}</Space>,
                  children: (
                    <Space direction="vertical" size={isMobile ? 'middle' : 'large'} style={{ width: '100%' }}>

                      {/* 默认配置提示 */}
                      {isDefaultSettings && (
                        <Alert
                          message={t('defaultAlert.message')}
                          description={
                            <div style={{ fontSize: isMobile ? '12px' : '14px' }}>
                              <p style={{ margin: '8px 0' }}>
                                <Trans ns="settings" i18nKey="defaultAlert.desc" components={{ code: <code /> }} />
                              </p>
                              <p style={{ margin: '8px 0 0 0' }}>
                                <Trans ns="settings" i18nKey="defaultAlert.saveNoteFull" components={{ code: <code /> }} />
                              </p>
                            </div>
                          }
                          type="info"
                          showIcon
                          style={{ marginBottom: isMobile ? 12 : 16 }}
                        />
                      )}

                      {/* 已保存配置提示 */}
                      {hasSettings && !isDefaultSettings && (
                        <Alert
                          message={t('defaultAlert.savedMessage')}
                          type="success"
                          showIcon
                          style={{ marginBottom: isMobile ? 12 : 16 }}
                        />
                      )}

                      {/* 表单 */}
                      <Spin spinning={initialLoading}>
                        <Form
                          form={form}
                          layout="vertical"
                          onFinish={handleSave}
                          autoComplete="off"
                        >
                          <Form.Item
                            label={
                              <Space size={4}>
                                <span>{t('form.apiProvider')}</span>
                                <InfoCircleOutlined
                                  title={t('form.apiProviderTooltip')}
                                  style={{ color: token.colorTextSecondary, fontSize: isMobile ? '12px' : '14px' }}
                                />
                              </Space>
                            }
                            name="api_provider"
                            rules={[{ required: true, message: t('validation.providerRequired') }]}
                          >
                            <Select size={isMobile ? 'middle' : 'large'} onChange={handleProviderChange}>
                              {apiProviders.map(provider => (
                                <Option key={provider.value} value={provider.value}>
                                  {provider.label}
                                </Option>
                              ))}
                            </Select>
                          </Form.Item>

                          {selectedProvider === 'xiaomi_mimo' && (
                            <Alert
                              type="info"
                              showIcon
                              message={t('provider.xiaomiMimoAdapter')}
                              description={t('provider.xiaomiMimoBuiltInDesc')}
                              style={{ marginBottom: 16 }}
                            />
                          )}

                          <Form.Item
                            label={
                              <Space size={4}>
                                <span>{t('form.apiKey')}</span>
                                <InfoCircleOutlined
                                  title={t('form.apiKeyTooltip')}
                                  style={{ color: token.colorTextSecondary, fontSize: isMobile ? '12px' : '14px' }}
                                />
                              </Space>
                            }
                            name="api_key"
                            rules={builtInKeyProviders.includes(selectedProvider) ? [] : [{ required: true, message: t('validation.apiKeyRequired') }]}
                          >
                            <Input.Password
                              size={isMobile ? 'middle' : 'large'}
                              placeholder={builtInKeyProviders.includes(selectedProvider) ? t('placeholder.apiKeyBuiltIn') : 'sk-...'}
                              autoComplete="new-password"
                              disabled={builtInKeyProviders.includes(selectedProvider)}
                            />
                          </Form.Item>

                          <Form.Item
                            label={
                              <Space size={4}>
                                <span>{t('form.apiBaseUrl')}</span>
                                <InfoCircleOutlined
                                  title={t('form.apiBaseUrlTooltip')}
                                  style={{ color: token.colorTextSecondary, fontSize: isMobile ? '12px' : '14px' }}
                                />
                              </Space>
                            }
                            name="api_base_url"
                            rules={[
                              { required: true, message: t('validation.baseUrlRequired') },
                              { type: 'url', message: t('validation.urlInvalid') }
                            ]}
                          >
                            <Input
                              size={isMobile ? 'middle' : 'large'}
                              placeholder="https://api.openai.com/v1"
                            />
                          </Form.Item>

                          <Form.Item
                            label={
                              <Space size={4}>
                                <span>{t('form.llmModel')}</span>
                                <InfoCircleOutlined
                                  title={t('form.llmModelTooltip')}
                                  style={{ color: token.colorTextSecondary, fontSize: isMobile ? '12px' : '14px' }}
                                />
                              </Space>
                            }
                            name="llm_model"
                            rules={[{ required: true, message: t('validation.modelRequired') }]}
                          >
                            <Select
                              size={isMobile ? 'middle' : 'large'}
                              showSearch
                              placeholder={isMobile ? t('placeholder.modelMobile') : t('presetModal.modelPlaceholder')}
                              optionFilterProp="label"
                              loading={fetchingModels}
                              onFocus={handleModelSelectFocus}
                              onSearch={(value) => setModelSearchText(value)}
                              onSelect={() => setModelSearchText('')}
                              onBlur={() => setModelSearchText('')}
                              filterOption={(input, option) => {
                                // 手动输入的选项始终显示
                                if (option?.value === input && !modelOptions.some(m => m.value === input)) return true;
                                return (option?.label ?? '').toLowerCase().includes(input.toLowerCase()) ||
                                  (option?.description ?? '').toLowerCase().includes(input.toLowerCase());
                              }}
                              dropdownRender={(menu) => (
                                <>
                                  {menu}
                                  {fetchingModels && (
                                    <div style={{ padding: '8px 12px', color: token.colorTextSecondary, textAlign: 'center', fontSize: isMobile ? '12px' : '14px' }}>
                                      <Spin size="small" /> {t('modelSelect.fetching')}
                                    </div>
                                  )}
                                  {!fetchingModels && modelOptions.length === 0 && modelsFetched && !modelSearchText && (
                                    <div style={{ padding: '8px 12px', color: token.colorError, textAlign: 'center', fontSize: isMobile ? '12px' : '14px' }}>
                                      {t('modelSelect.fetchEmpty')}
                                    </div>
                                  )}
                                  {!fetchingModels && modelOptions.length === 0 && !modelsFetched && !modelSearchText && (
                                    <div style={{ padding: '8px 12px', color: token.colorTextSecondary, textAlign: 'center', fontSize: isMobile ? '12px' : '14px' }}>
                                      {t('modelSelect.clickToFetch')}
                                    </div>
                                  )}
                                </>
                              )}
                              notFoundContent={
                                fetchingModels ? (
                                  <div style={{ padding: '8px 12px', textAlign: 'center', fontSize: isMobile ? '12px' : '14px' }}>
                                    <Spin size="small" /> {t('modelSelect.loading')}
                                  </div>
                                ) : null
                              }
                              suffixIcon={
                                !isMobile ? (
                                  <div
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      if (!fetchingModels) {
                                        setModelsFetched(false);
                                        handleFetchModels(false);
                                      }
                                    }}
                                    style={{
                                      cursor: fetchingModels ? 'not-allowed' : 'pointer',
                                      display: 'flex',
                                      alignItems: 'center',
                                      padding: '0 4px',
                                      height: '100%',
                                      marginRight: -8
                                    }}
                                    title={t('modelSelect.refreshTooltip')}
                                  >
                                    <Button
                                      type="text"
                                      size="small"
                                      icon={<ReloadOutlined />}
                                      loading={fetchingModels}
                                      style={{ pointerEvents: 'none' }}
                                    >
                                      {t('buttons.refresh')}
                                    </Button>
                                  </div>
                                ) : undefined
                              }
                              options={(() => {
                                const providerDefaultModels = selectedProvider === 'xiaomi_mimo' ? xiaomiMimoDefaultModels : [];
                                const combinedModels = [
                                  ...providerDefaultModels,
                                  ...modelOptions.filter(model => !providerDefaultModels.some(item => item.value === model.value)),
                                ];
                                const opts = combinedModels.map(model => ({
                                  value: model.value,
                                  label: model.label,
                                  description: model.description
                                }));
                                // 如果用户输入了文本且不在已有选项中，添加手动输入选项
                                if (modelSearchText && !modelOptions.some(m =>
                                  m.value.toLowerCase() === modelSearchText.toLowerCase() ||
                                  m.label.toLowerCase() === modelSearchText.toLowerCase()
                                )) {
                                  opts.unshift({
                                    value: modelSearchText,
                                    label: modelSearchText,
                                    description: t('modelSelect.manualDesc')
                                  });
                                }
                                return opts;
                              })()}
                              optionRender={(option) => (
                                <div>
                                  <div style={{ fontWeight: 500, fontSize: isMobile ? '13px' : '14px' }}>
                                    {option.data.description === t('modelSelect.manualDesc') ? (
                                      <Space size={4}>
                                        <EditOutlined style={{ color: token.colorPrimary }} />
                                        <span>{t('modelSelect.useTyped', { name: option.data.label })}</span>
                                      </Space>
                                    ) : option.data.label}
                                  </div>
                                  {option.data.description && option.data.description !== t('modelSelect.manualDesc') && (
                                    <div style={{ fontSize: isMobile ? '11px' : '12px', color: token.colorTextTertiary, marginTop: '2px' }}>
                                      {option.data.description}
                                    </div>
                                  )}
                                </div>
                              )}
                            />
                          </Form.Item>

                          <Form.Item
                            label={
                              <Space size={4}>
                                <span>{t('form.temperature')}</span>
                                <InfoCircleOutlined
                                  title={t('form.temperatureTooltip')}
                                  style={{ color: token.colorTextSecondary, fontSize: isMobile ? '12px' : '14px' }}
                                />
                              </Space>
                            }
                            name="temperature"
                          >
                            <Slider
                              min={0}
                              max={2}
                              step={0.1}
                              marks={{
                                0: { style: { fontSize: isMobile ? '11px' : '12px' }, label: '0.0' },
                                0.7: { style: { fontSize: isMobile ? '11px' : '12px' }, label: '0.7' },
                                1: { style: { fontSize: isMobile ? '11px' : '12px' }, label: '1.0' },
                                2: { style: { fontSize: isMobile ? '11px' : '12px' }, label: '2.0' }
                              }}
                            />
                          </Form.Item>

                          <Form.Item
                            label={
                              <Space size={4}>
                                <span>{t('form.maxTokens')}</span>
                                <InfoCircleOutlined
                                  title={t('form.maxTokensTooltip')}
                                  style={{ color: token.colorTextSecondary, fontSize: isMobile ? '12px' : '14px' }}
                                />
                              </Space>
                            }
                            name="max_tokens"
                            rules={[
                              { required: true, message: t('validation.maxTokensRequired') },
                              { type: 'number', min: 1, message: t('validation.positiveNumber') }
                            ]}
                          >
                            <InputNumber
                              size={isMobile ? 'middle' : 'large'}
                              style={{ width: '100%' }}
                              min={1}
                              placeholder="2000"
                            />
                          </Form.Item>

                          <Form.Item
                            label={
                              <Space size={4}>
                                <span>{t('form.disableThinking')}</span>
                                <InfoCircleOutlined
                                  title={t('form.disableThinkingTooltip')}
                                  style={{ color: token.colorTextSecondary, fontSize: isMobile ? '12px' : '14px' }}
                                />
                              </Space>
                            }
                            name="disable_thinking"
                            valuePropName="checked"
                          >
                            <Switch />
                          </Form.Item>

                          <Form.Item
                            label={
                              <Space size={4}>
                                <span>{t('form.systemPrompt')}</span>
                                <InfoCircleOutlined
                                  title={t('form.systemPromptTooltip')}
                                  style={{ color: token.colorTextSecondary, fontSize: isMobile ? '12px' : '14px' }}
                                />
                              </Space>
                            }
                            name="system_prompt"
                          >
                            <TextArea
                              rows={4}
                              placeholder={t('placeholder.systemPrompt')}
                              maxLength={10000}
                              showCount
                              style={{ fontSize: isMobile ? '13px' : '14px' }}
                            />
                          </Form.Item>

                          {/* 测试结果展示 */}
                          {showTestResult && testResult && (
                            <Alert
                              message={
                                <Space>
                                  {testResult.success ? (
                                    <CheckCircleOutlined style={{ color: token.colorSuccess, fontSize: isMobile ? '16px' : '18px' }} />
                                  ) : (
                                    <CloseCircleOutlined style={{ color: token.colorError, fontSize: isMobile ? '16px' : '18px' }} />
                                  )}
                                  <span style={{ fontSize: isMobile ? '14px' : '16px', fontWeight: 500 }}>
                                    {testResult.message}
                                  </span>
                                </Space>
                              }
                              description={
                                <div style={{ marginTop: 8 }}>
                                  {testResult.success ? (
                                    <Space direction="vertical" size="small" style={{ width: '100%' }}>
                                      {testResult.response_time_ms && (
                                        <div style={{ fontSize: isMobile ? '12px' : '14px' }}>
                                          {t('testResult.responseTime')} <strong>{testResult.response_time_ms} ms</strong>
                                        </div>
                                      )}
                                      {testResult.response_preview && (
                                        <div style={{
                                          fontSize: isMobile ? '12px' : '13px',
                                          padding: '8px 12px',
                                          background: token.colorSuccessBg,
                                          borderRadius: '4px',
                                          border: `1px solid ${token.colorSuccessBorder}`,
                                          marginTop: '8px'
                                        }}>
                                          <div style={{ marginBottom: '4px', fontWeight: 500 }}>{t('testResult.previewLabel')}</div>
                                          <div style={{ color: token.colorTextSecondary }}>{testResult.response_preview}</div>
                                        </div>
                                      )}
                                      <div style={{ color: token.colorSuccess, fontSize: isMobile ? '12px' : '13px', marginTop: '4px' }}>
                                        {t('testResult.configOk')}
                                      </div>
                                    </Space>
                                  ) : (
                                    <Space direction="vertical" size="small" style={{ width: '100%' }}>
                                      {testResult.error && (
                                        <div style={{
                                          fontSize: isMobile ? '12px' : '13px',
                                          padding: '8px 12px',
                                          background: token.colorErrorBg,
                                          borderRadius: '4px',
                                          border: `1px solid ${token.colorErrorBorder}`,
                                          color: token.colorError
                                        }}>
                                          <strong>{t('presets.errorLabel')}</strong> {testResult.error}
                                        </div>
                                      )}
                                      {testResult.error_type && (
                                        <div style={{ fontSize: isMobile ? '11px' : '12px', color: token.colorTextSecondary }}>
                                          {t('testResult.errorType')} {testResult.error_type}
                                        </div>
                                      )}
                                      {testResult.suggestions && testResult.suggestions.length > 0 && (
                                        <div style={{ marginTop: '8px' }}>
                                          <div style={{ fontSize: isMobile ? '12px' : '13px', fontWeight: 500, marginBottom: '4px' }}>
                                            {t('testResult.suggestionsLabel')}
                                          </div>
                                          <ul style={{
                                            margin: 0,
                                            paddingLeft: isMobile ? '16px' : '20px',
                                            fontSize: isMobile ? '12px' : '13px',
                                            color: token.colorTextSecondary
                                          }}>
                                            {testResult.suggestions.map((suggestion, index) => (
                                              <li key={index} style={{ marginBottom: '4px' }}>{suggestion}</li>
                                            ))}
                                          </ul>
                                        </div>
                                      )}
                                    </Space>
                                  )}
                                </div>
                              }
                              type={testResult.success ? 'success' : 'error'}
                              closable
                              onClose={() => setShowTestResult(false)}
                              style={{ marginBottom: isMobile ? 16 : 24 }}
                            />
                          )}

                          {/* 操作按钮 */}
                          <Form.Item style={{ marginBottom: 0, marginTop: isMobile ? 24 : 32 }}>
                            {isMobile ? (
                              // 移动端：垂直堆叠布局
                              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                                <Button
                                  type="primary"
                                  size="large"
                                  icon={<SaveOutlined />}
                                  htmlType="submit"
                                  loading={loading}
                                  block
                                  style={{
                                    background: token.colorPrimary,
                                    border: 'none',
                                    height: '44px'
                                  }}
                                >
                                  {t('buttons.saveSettings')}
                                </Button>
                                <Button
                                  size="large"
                                  icon={<ThunderboltOutlined />}
                                  onClick={handleTestConnection}
                                  loading={testingApi}
                                  block
                                  style={{
                                    borderColor: token.colorSuccess,
                                    color: token.colorSuccess,
                                    fontWeight: 500,
                                    height: '44px'
                                  }}
                                >
                                  {testingApi ? t('buttons.testing') : t('buttons.testConnection')}
                                </Button>
                                <Space size="middle" style={{ width: '100%' }}>
                                  <Button
                                    size="large"
                                    icon={<ReloadOutlined />}
                                    onClick={handleReset}
                                    style={{ flex: 1, height: '44px' }}
                                  >
                                    {t('buttons.reset')}
                                  </Button>
                                  {hasSettings && (
                                    <Button
                                      danger
                                      size="large"
                                      icon={<DeleteOutlined />}
                                      onClick={handleDelete}
                                      loading={loading}
                                      style={{ flex: 1, height: '44px' }}
                                    >
                                      {t('buttons.delete')}
                                    </Button>
                                  )}
                                </Space>
                              </Space>
                            ) : (
                              // 桌面端：删除在左边，测试、重置和保存在右边
                              <div style={{
                                display: 'flex',
                                justifyContent: 'space-between',
                                alignItems: 'center',
                                gap: '16px',
                                flexWrap: 'wrap'
                              }}>
                                {/* 左侧：删除按钮 */}
                                {hasSettings ? (
                                  <Button
                                    danger
                                    size="large"
                                    icon={<DeleteOutlined />}
                                    onClick={handleDelete}
                                    loading={loading}
                                    style={{
                                      minWidth: '100px'
                                    }}
                                  >
                                    {t('buttons.deleteConfig')}
                                  </Button>
                                ) : (
                                  <div /> // 占位符，保持右侧按钮位置
                                )}

                                {/* 右侧：测试、重置和保存按钮组 */}
                                <Space size="middle">
                                  <Button
                                    size="large"
                                    icon={<ThunderboltOutlined />}
                                    onClick={handleTestConnection}
                                    loading={testingApi}
                                    style={{
                                      borderColor: token.colorSuccess,
                                      color: token.colorSuccess,
                                      fontWeight: 500,
                                      minWidth: '100px'
                                    }}
                                  >
                                    {testingApi ? t('buttons.testing') : t('buttons.test')}
                                  </Button>
                                  <Button
                                    size="large"
                                    icon={<ReloadOutlined />}
                                    onClick={handleReset}
                                    style={{
                                      minWidth: '100px'
                                    }}
                                  >
                                    {t('buttons.reset')}
                                  </Button>
                                  <Button
                                    type="primary"
                                    size="large"
                                    icon={<SaveOutlined />}
                                    htmlType="submit"
                                    loading={loading}
                                    style={{
                                      background: token.colorPrimary,
                                      border: 'none',
                                      minWidth: '120px',
                                      fontWeight: 500
                                    }}
                                  >
                                    {t('buttons.save')}
                                  </Button>
                                </Space>
                              </div>
                            )}
                          </Form.Item>
                        </Form>
                      </Spin>
                    </Space>
                  ),
                },
                {
                  key: 'cover',
                  label: <Space size={6}><PictureOutlined />{t('tabs.cover')}</Space>,
                  children: (
                    <Spin spinning={initialLoading}>
                      <Form form={form} layout="vertical" onFinish={handleSave} autoComplete="off">

                        <Form.Item label={t('cover.enabledLabel')} name="cover_enabled" style={{ marginBottom: 16 }}>
                          <Select
                            size={isMobile ? 'middle' : 'large'}
                            onChange={() => setCoverTestResult(null)}
                            options={[
                              { value: true, label: t('cover.enableOption') },
                              { value: false, label: t('cover.disableOption') },
                            ]}
                          />
                        </Form.Item>

                        <Form.Item label={t('cover.providerLabel')} name="cover_api_provider" rules={[{ required: true, message: t('cover.providerRequired') }]}>
                          <Select size={isMobile ? 'middle' : 'large'} onChange={handleCoverProviderChange}>
                            {coverApiProviders.map(provider => (
                              <Option key={provider.value} value={provider.value}>{provider.label}</Option>
                            ))}
                          </Select>
                        </Form.Item>

                        <Form.Item label={t('cover.apiKeyLabel')} name="cover_api_key" rules={[{ required: true, message: t('cover.apiKeyRequired') }]}>
                          <Input.Password size={isMobile ? 'middle' : 'large'} placeholder={t('cover.apiKeyPlaceholder')} autoComplete="new-password" />
                        </Form.Item>

                        <Form.Item label={t('cover.baseUrlLabel')} name="cover_api_base_url" rules={[{ type: 'url', message: t('validation.urlInvalid') }]}>
                          <Input size={isMobile ? 'middle' : 'large'} placeholder={selectedCoverProvider === 'grok' ? 'https://api.x.ai/v1' : 'https://generativelanguage.googleapis.com/v1beta'} />
                        </Form.Item>

                        <Form.Item label={t('cover.modelLabel')} name="cover_image_model" rules={[{ required: true, message: t('cover.modelRequired') }]}>
                          <Input
                            size={isMobile ? 'middle' : 'large'}
                            placeholder={selectedCoverProvider === 'grok'
                              ? 'grok-2-image'
                              : 'gemini-2.0-flash-exp-image-generation'}
                          />
                        </Form.Item>

                        {coverTestResult && (
                          <Alert
                            type={coverTestResult.success ? 'success' : 'error'}
                            showIcon
                            message={coverTestResult.message}
                            description={coverTestResult.success ? `Provider: ${coverTestResult.provider || '-'} / Model: ${coverTestResult.model || '-'}` : undefined}
                            style={{ marginBottom: 16 }}
                          />
                        )}

                        <Form.Item style={{ marginBottom: 0, marginTop: 24 }}>
                          <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
                            <Space wrap>
                              <Button
                                icon={<ThunderboltOutlined />}
                                onClick={handleCoverTestConnection}
                                loading={testingCoverApi}
                                style={{ borderColor: token.colorSuccess, color: token.colorSuccess, fontWeight: 500 }}
                              >
                                {testingCoverApi ? t('buttons.testing') : t('cover.testButton')}
                              </Button>
                              <Button icon={<ReloadOutlined />} onClick={handleReset}>{t('buttons.reset')}</Button>
                            </Space>
                            <Button type="primary" icon={<SaveOutlined />} htmlType="submit" loading={loading}>{t('cover.saveButton')}</Button>
                          </Space>
                        </Form.Item>
                      </Form>
                    </Spin>
                  ),
                },
                {
                  key: 'presets',
                  label: <Space size={6}><CopyOutlined />{t('tabs.presets')}</Space>,
                  children: renderPresetsList(),
                },
              ]}
            />
          </Card>
        </div>

        {/* 预设编辑对话框 */}
        <Modal
          title={editingPreset ? t('presetModal.editTitle') : t('presetModal.createTitle')}
          open={isPresetModalVisible}
          onOk={handlePresetSave}
          onCancel={handlePresetCancel}
          width={isMobile ? '95%' : 640}
          centered
          okText={t('buttons.save')}
          cancelText={t('confirm.cancel')}
          styles={{
            body: {
              padding: isMobile ? '16px' : '20px 24px'
            }
          }}
        >
          <Form
            form={presetForm}
            layout="vertical"
            size={isMobile ? 'middle' : 'large'}
          >
            {/* 基本信息 */}
            <Row gutter={16}>
              <Col xs={24} sm={16}>
                <Form.Item
                  name="name"
                  label={t('presetModal.nameLabel')}
                  rules={[
                    { required: true, message: t('presetModal.nameRequired') },
                    { max: 50, message: t('presetModal.nameMax') },
                  ]}
                  style={{ marginBottom: 16 }}
                >
                  <Input placeholder={t('presetModal.namePlaceholder')} />
                </Form.Item>
              </Col>
              <Col xs={24} sm={8}>
                <Form.Item
                  name="api_provider"
                  label={t('form.apiProvider')}
                  rules={[{ required: true, message: t('presetModal.providerRequired') }]}
                  style={{ marginBottom: 16 }}
                >
                  <Select placeholder={t('presetModal.providerPlaceholder')} onChange={handlePresetProviderChange}>
                    <Select.Option value="xiaomi_mimo">{t('provider.xiaomiMimoBuiltIn')}</Select.Option>
                    <Select.Option value="openai">OpenAI</Select.Option>
                    <Select.Option value="gemini">Google Gemini</Select.Option>
                  </Select>
                </Form.Item>

                {selectedPresetProvider === 'xiaomi_mimo' && (
                  <Alert
                    type="info"
                    showIcon
                    message={t('provider.xiaomiMimoAdapter')}
                    description={t('provider.xiaomiMimoPresetDesc')}
                    style={{ marginBottom: 16 }}
                  />
                )}
              </Col>
            </Row>

            <Form.Item
              name="description"
              label={t('presetModal.descriptionLabel')}
              rules={[{ max: 200, message: t('presetModal.descriptionMax') }]}
              style={{ marginBottom: 16 }}
            >
              <Input placeholder={t('presetModal.descriptionPlaceholder')} />
            </Form.Item>

            {/* API 配置 */}
            <Row gutter={16}>
              <Col xs={24} sm={12}>
                <Form.Item
                  name="api_key"
                  label="API Key"
                  rules={builtInKeyProviders.includes(selectedPresetProvider) ? [] : [{ required: true, message: t('presetModal.apiKeyRequired') }]}
                  style={{ marginBottom: 16 }}
                >
                  <Input.Password
                    placeholder={builtInKeyProviders.includes(selectedPresetProvider) ? t('presetModal.apiKeyBuiltInPlaceholder') : 'sk-...'}
                    disabled={builtInKeyProviders.includes(selectedPresetProvider)}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12}>
                <Form.Item
                  name="api_base_url"
                  label="API Base URL"
                  style={{ marginBottom: 16 }}
                >
                  <Input placeholder="https://api.openai.com/v1" />
                </Form.Item>
              </Col>
            </Row>

            {/* 模型配置 */}
            <Row gutter={16}>
              <Col xs={24} sm={12}>
                <Form.Item
                  name="llm_model"
                  label={
                    <Space size={4}>
                      <span>{t('form.llmModel')}</span>
                      <InfoCircleOutlined
                        title={t('presetModal.modelTooltip')}
                        style={{ color: token.colorTextSecondary, fontSize: '12px' }}
                      />
                    </Space>
                  }
                  rules={[{ required: true, message: t('presetModal.modelRequired') }]}
                  style={{ marginBottom: 16 }}
                >
                  <Select
                    showSearch
                    placeholder={t('presetModal.modelPlaceholder')}
                    optionFilterProp="label"
                    loading={fetchingPresetModels}
                    onFocus={handlePresetModelSelectFocus}
                    onSearch={(value) => setPresetModelSearchText(value)}
                    onSelect={() => setPresetModelSearchText('')}
                    onBlur={() => setPresetModelSearchText('')}
                    filterOption={(input, option) => {
                      // 手动输入的选项始终显示
                      if (option?.value === input && !presetModelOptions.some(m => m.value === input)) return true;
                      return (option?.label ?? '').toLowerCase().includes(input.toLowerCase()) ||
                        (option?.description ?? '').toLowerCase().includes(input.toLowerCase());
                    }}
                    dropdownRender={(menu) => (
                      <>
                        {menu}
                        {fetchingPresetModels && (
                          <div style={{ padding: '8px 12px', color: token.colorTextSecondary, textAlign: 'center', fontSize: '12px' }}>
                            <Spin size="small" /> {t('modelSelect.fetching')}
                          </div>
                        )}
                        {!fetchingPresetModels && presetModelOptions.length === 0 && presetModelsFetched && !presetModelSearchText && (
                          <div style={{ padding: '8px 12px', color: token.colorError, textAlign: 'center', fontSize: '12px' }}>
                            {t('modelSelect.fetchEmpty')}
                          </div>
                        )}
                        {!fetchingPresetModels && presetModelOptions.length === 0 && !presetModelsFetched && !presetModelSearchText && (
                          <div style={{ padding: '8px 12px', color: token.colorTextSecondary, textAlign: 'center', fontSize: '12px' }}>
                            {t('modelSelect.clickToFetch')}
                          </div>
                        )}
                      </>
                    )}
                    notFoundContent={
                      fetchingPresetModels ? (
                        <div style={{ padding: '8px 12px', textAlign: 'center', fontSize: '12px' }}>
                          <Spin size="small" /> {t('modelSelect.loading')}
                        </div>
                      ) : null
                    }
                    suffixIcon={
                      <div
                        onClick={(e) => {
                          e.stopPropagation();
                          if (!fetchingPresetModels) {
                            setPresetModelsFetched(false);
                            handleFetchPresetModels(false);
                          }
                        }}
                        style={{
                          cursor: fetchingPresetModels ? 'not-allowed' : 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          padding: '0 4px',
                          height: '100%',
                          marginRight: -8
                        }}
                        title={t('presetModal.fetchTooltip')}
                      >
                        <Button
                          type="text"
                          size="small"
                          icon={<ReloadOutlined />}
                          loading={fetchingPresetModels}
                          style={{ pointerEvents: 'none' }}
                        >
                          {t('buttons.fetch')}
                        </Button>
                      </div>
                    }
                    options={(() => {
                      const providerDefaultModels = selectedPresetProvider === 'xiaomi_mimo' ? xiaomiMimoDefaultModels : [];
                      const combinedModels = [
                        ...providerDefaultModels,
                        ...presetModelOptions.filter(model => !providerDefaultModels.some(item => item.value === model.value)),
                      ];
                      const opts = combinedModels.map(model => ({
                        value: model.value,
                        label: model.label,
                        description: model.description
                      }));
                      // 如果用户输入了文本且不在已有选项中，添加手动输入选项
                      if (presetModelSearchText && !presetModelOptions.some(m =>
                        m.value.toLowerCase() === presetModelSearchText.toLowerCase() ||
                        m.label.toLowerCase() === presetModelSearchText.toLowerCase()
                      )) {
                        opts.unshift({
                          value: presetModelSearchText,
                          label: presetModelSearchText,
                          description: t('modelSelect.manualDesc')
                        });
                      }
                      return opts;
                    })()}
                    optionRender={(option) => (
                      <div>
                        <div style={{ fontWeight: 500, fontSize: '13px' }}>
                          {option.data.description === t('modelSelect.manualDesc') ? (
                            <Space size={4}>
                              <EditOutlined style={{ color: token.colorPrimary }} />
                              <span>{t('modelSelect.useTyped', { name: option.data.label })}</span>
                            </Space>
                          ) : option.data.label}
                        </div>
                        {option.data.description && option.data.description !== t('modelSelect.manualDesc') && (
                          <div style={{ fontSize: '11px', color: token.colorTextTertiary, marginTop: '2px' }}>
                            {option.data.description}
                          </div>
                        )}
                      </div>
                    )}
                  />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6}>
                <Form.Item
                  name="temperature"
                  label={t('presetModal.temperatureLabel')}
                  rules={[{ required: true, message: t('presetModal.required') }]}
                  style={{ marginBottom: 16 }}
                >
                  <InputNumber
                    min={0}
                    max={2}
                    step={0.1}
                    style={{ width: '100%' }}
                    placeholder="0.7"
                  />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6}>
                <Form.Item
                  name="max_tokens"
                  label={t('presetModal.maxTokensLabel')}
                  rules={[{ required: true, message: t('presetModal.required') }]}
                  style={{ marginBottom: 16 }}
                >
                  <InputNumber
                    min={1}
                    max={100000}
                    style={{ width: '100%' }}
                    placeholder="32000"
                  />
                </Form.Item>
              </Col>
            </Row>

            <Form.Item
              name="system_prompt"
              label={t('form.systemPrompt')}
              style={{ marginBottom: 0 }}
            >
              <TextArea
                rows={isMobile ? 2 : 3}
                placeholder={t('presetModal.systemPromptPlaceholder')}
                maxLength={10000}
                showCount
              />
            </Form.Item>
          </Form>
        </Modal>
      </div>
    </>
  );
}
