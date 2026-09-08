import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Card, Input, Button, Space, Typography, message, Spin, Modal, theme } from 'antd';
import { SendOutlined, ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons';
import { inspirationApi } from '../services/api';
import { AIProjectGenerator, type GenerationConfig } from '../components/AIProjectGenerator';

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;

type Step = 'idea' | 'title' | 'description' | 'theme' | 'genre' | 'perspective' | 'outline_mode' | 'confirm' | 'generating' | 'complete';

interface FailedRequest {
  step: 'title' | 'description' | 'theme' | 'genre';
  context: Partial<WizardData>;
}

interface Message {
  type: 'ai' | 'user';
  content: string;
  options?: string[];
  isMultiSelect?: boolean;
  optionsDisabled?: boolean; // 标记选项是否已禁用
  canRefine?: boolean; // 是否可以优化（用于支持多轮对话）
  step?: Step; // 当前步骤（用于反馈）
  failedRequest?: FailedRequest; // 将失败操作绑定到消息，避免历史按钮重试错误请求
}

interface WizardData {
  title: string;
  description: string;
  theme: string;
  genre: string[];
  narrative_perspective: string;
  outline_mode: 'one-to-one' | 'one-to-many';
}

// 缓存数据接口
interface CacheData {
  messages: Message[];
  currentStep: Step;
  wizardData: Partial<WizardData>;
  initialIdea: string;
  selectedOptions: string[];
  lastFailedRequest: FailedRequest | null;
  timestamp: number;
}

// 缓存键
const CACHE_KEY = 'inspiration_conversation_cache';
// 缓存有效期：24小时
const CACHE_EXPIRY = 24 * 60 * 60 * 1000;

const Inspiration: React.FC = () => {
  const { t } = useTranslation('inspiration');
  const navigate = useNavigate();
  const [currentStep, setCurrentStep] = useState<Step>('idea');
  const [isMobile, setIsMobile] = useState(window.innerWidth <= 768);
  const { token } = theme.useToken();

  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth <= 768);
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  const [messages, setMessages] = useState<Message[]>([
    {
      type: 'ai',
      content: t('greeting.welcome'),
    }
  ]);
  const [inputValue, setInputValue] = useState('');
  const [loading, setLoading] = useState(false);
  const [selectedOptions, setSelectedOptions] = useState<string[]>([]);

  // 收集的数据
  const [wizardData, setWizardData] = useState<Partial<WizardData>>({});
  // 保存用户的原始想法，用于保持上下文一致性
  const [initialIdea, setInitialIdea] = useState<string>('');
  
  // 反馈相关状态
  const [feedbackValue, setFeedbackValue] = useState('');
  const [showFeedbackInput, setShowFeedbackInput] = useState<number | null>(null); // 当前显示反馈输入的消息索引
  const [refining, setRefining] = useState(false); // 正在优化选项

  // 生成配置
  const [generationConfig, setGenerationConfig] = useState<GenerationConfig | null>(null);

  // Modal hook
  const [modal, contextHolder] = Modal.useModal();

  // 滚动容器引用
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const chatContainerRef = useRef<HTMLDivElement>(null);
  const sessionVersionRef = useRef(0);

  // 记录上次失败的请求参数，用于重试
  const [lastFailedRequest, setLastFailedRequest] = useState<FailedRequest | null>(null);

  // 标记是否已经加载缓存
  const [cacheLoaded, setCacheLoaded] = useState(false);

  // ==================== 缓存管理函数 ====================

  // 清除缓存
  const clearCache = useCallback(() => {
    try {
      localStorage.removeItem(CACHE_KEY);
      console.log('🗑️ 缓存已清除');
    } catch (error) {
      console.error('清除缓存失败:', error);
    }
  }, []);

  // 保存到缓存
  const saveToCache = useCallback(() => {
    try {
      // 只在对话阶段保存，生成阶段不保存
      if (currentStep === 'generating' || currentStep === 'complete') {
        return;
      }

      // 只有用户有输入时才保存（至少两条消息：AI问候+用户回复）
      if (messages.length <= 1) {
        return;
      }

      const cacheData: CacheData = {
        messages,
        currentStep,
        wizardData,
        initialIdea,
        selectedOptions,
        lastFailedRequest,
        timestamp: Date.now()
      };

      localStorage.setItem(CACHE_KEY, JSON.stringify(cacheData));
      console.log('💾 对话已自动保存');
    } catch (error) {
      console.error('保存缓存失败:', error);
    }
  }, [currentStep, messages, wizardData, initialIdea, selectedOptions, lastFailedRequest]);

  // 从缓存恢复
  const restoreFromCache = useCallback((): boolean => {
    try {
      const cached = localStorage.getItem(CACHE_KEY);
      if (!cached) {
        return false;
      }

      const cacheData: CacheData = JSON.parse(cached);
      const age = Date.now() - cacheData.timestamp;

      // 检查缓存是否过期
      if (age > CACHE_EXPIRY) {
        console.log('⏰ 缓存已过期，清除');
        clearCache();
        return false;
      }

      // 必须有有效的对话数据
      if (!cacheData.messages || cacheData.messages.length <= 1) {
        return false;
      }

      // 恢复所有状态
      setMessages(cacheData.messages);
      setCurrentStep(cacheData.currentStep);
      setWizardData(cacheData.wizardData);
      setInitialIdea(cacheData.initialIdea);
      setSelectedOptions(cacheData.selectedOptions);
      // 恢复失败请求信息，确保"重新生成"按钮可用
      if (cacheData.lastFailedRequest) {
        setLastFailedRequest(cacheData.lastFailedRequest);
      }

      console.log('✅ 已恢复上次的对话进度');
      message.success(t('toast.restored'), 2);
      return true;
    } catch (error) {
      console.error('恢复缓存失败:', error);
      clearCache();
      return false;
    }
  }, [clearCache]);

  // ==================== 组件挂载时恢复缓存 ====================

  useEffect(() => {
    if (!cacheLoaded) {
      restoreFromCache();
      setCacheLoaded(true);
    }
  }, [cacheLoaded, restoreFromCache]);

  // ==================== 自动保存：状态变化时保存 ====================

  useEffect(() => {
    // 防抖保存
    const timer = setTimeout(() => {
      if (cacheLoaded) {
        saveToCache();
      }
    }, 500);

    return () => clearTimeout(timer);
  }, [messages, currentStep, wizardData, initialIdea, selectedOptions, lastFailedRequest, cacheLoaded, saveToCache]);

  // 自动滚动到底部
  const scrollToBottom = () => {
    setTimeout(() => {
      if (chatContainerRef.current) {
        chatContainerRef.current.scrollTo({
          top: chatContainerRef.current.scrollHeight,
          behavior: 'smooth'
        });
      }
    }, 100);
  };

  // 当消息更新时自动滚动
  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // 重试生成
  const handleRetry = async (failedRequest: FailedRequest | null = lastFailedRequest) => {
    if (!failedRequest || loading) return;

    const sessionVersion = sessionVersionRef.current;
    setLoading(true);
    try {
      const response = await inspirationApi.generateOptions({
        step: failedRequest.step,
        context: failedRequest.context
      });

      if (sessionVersion !== sessionVersionRef.current) return;

      if (response.error) {
        message.error(response.error);
        return;
      }

      setMessages(prev => {
        const newMessages = [...prev];
        let failedMessageIndex = -1;
        for (let index = newMessages.length - 1; index >= 0; index -= 1) {
          if (newMessages[index].failedRequest?.step === failedRequest.step) {
            failedMessageIndex = index;
            break;
          }
        }
        if (failedMessageIndex >= 0) {
          newMessages.splice(failedMessageIndex, 1);
        }
        return newMessages;
      });

      const aiMessage: Message = {
        type: 'ai',
        content: response.prompt || t('prompt.chooseOption'),
        options: response.options || [],
        isMultiSelect: failedRequest.step === 'genre',
        canRefine: true,
        step: failedRequest.step,
      };
      setMessages(prev => [...prev, aiMessage]);
      setCurrentStep(failedRequest.step);
      setLastFailedRequest(null);
    } catch (error: unknown) {
      console.error('重试失败:', error);
      message.error(t('error.retryFailed'));
    } finally {
      if (sessionVersion === sessionVersionRef.current) setLoading(false);
    }
  };

  // 处理用户反馈，重新生成选项
  const handleRefineOptions = async (messageIndex: number, feedback: string) => {
    if (!feedback.trim() || refining || loading) {
      message.warning(t('toast.enterFeedback'));
      return;
    }

    const targetMessage = messages[messageIndex];
    if (!targetMessage.options || !targetMessage.step) {
      return;
    }

    const sessionVersion = sessionVersionRef.current;
    setRefining(true);
    setShowFeedbackInput(null);
    setFeedbackValue('');

    // 先禁用旧的选项
    setMessages(prev => {
      const newMessages = [...prev];
      if (newMessages[messageIndex]) {
        newMessages[messageIndex] = {
          ...newMessages[messageIndex],
          optionsDisabled: true,
          canRefine: false, // 同时禁用反馈功能
        };
      }
      return newMessages;
    });

    try {
      // 添加用户反馈消息
      const feedbackMessage: Message = {
        type: 'user',
        content: t('aiMessage.feedback', { feedback }),
      };
      setMessages(prev => [...prev, feedbackMessage]);

      const step = targetMessage.step as 'title' | 'description' | 'theme' | 'genre';
      
      // 构建上下文
      const context: Partial<WizardData> & { initial_idea?: string } = {
        initial_idea: initialIdea,
        title: wizardData.title,
        description: wizardData.description,
        theme: wizardData.theme,
      };

      // 调用refine接口
      const response = await inspirationApi.refineOptions({
        step,
        context,
        feedback,
        previous_options: targetMessage.options,
      });

      if (sessionVersion !== sessionVersionRef.current) return;

      if (response.error) {
        setMessages(prev => prev.map((item, index) => index === messageIndex
          ? { ...item, optionsDisabled: false, canRefine: true }
          : item));
        setFeedbackValue(feedback);
        setShowFeedbackInput(messageIndex);
        message.error(response.error);
        return;
      }

      // 添加新的AI消息
      const aiMessage: Message = {
        type: 'ai',
        content: response.prompt || t('refine.prompt', { type: step === 'title' ? t('refine.typeTitle') : step === 'description' ? t('refine.typeDesc') : step === 'theme' ? t('refine.typeTheme') : t('refine.typeGenre') }),
        options: response.options || [],
        isMultiSelect: step === 'genre',
        canRefine: true,
        step: step,
      };
      setMessages(prev => [...prev, aiMessage]);

      message.success(t('refine.success'));
    } catch (error: unknown) {
      if (sessionVersion !== sessionVersionRef.current) return;
      setMessages(prev => prev.map((item, index) => index === messageIndex
        ? { ...item, optionsDisabled: false, canRefine: true }
        : item));
      setFeedbackValue(feedback);
      setShowFeedbackInput(messageIndex);
      console.error('优化选项失败:', error);
      const errMsg = error instanceof Error ? error.message : t('error.optimizeFailed');
      const axiosError = error as { response?: { data?: { detail?: string } } };
      message.error(axiosError.response?.data?.detail || errMsg);
    } finally {
      if (sessionVersion === sessionVersionRef.current) setRefining(false);
    }
  };

  // 步骤顺序
  const stepOrder: Step[] = ['idea', 'title', 'description', 'theme', 'genre', 'perspective', 'outline_mode', 'confirm'];

  const handleSendMessage = async () => {
    if (!inputValue.trim() || loading || refining) {
      message.warning(t('toast.enterContent'));
      return;
    }

    const userMessage: Message = {
      type: 'user',
      content: inputValue,
    };
    setMessages(prev => [...prev, userMessage]);

    const userInput = inputValue;
    setInputValue('');
    const sessionVersion = sessionVersionRef.current;
    setLoading(true);

    try {
      if (currentStep === 'idea') {
        setInitialIdea(userInput);

        const requestData = {
          step: 'title' as const,
          context: {
            initial_idea: userInput,
            description: userInput
          }
        };

        const response = await inspirationApi.generateOptions(requestData);

        if (sessionVersion !== sessionVersionRef.current) return;

        if (response.error || !response.options || response.options.length < 3) {
          const errorMessage: Message = {
            type: 'ai',
            content: response.error
              ? `${t('error.titleError', { msg: response.error })}${t('error.promptContinuation')}`
              : `${t('error.invalidOptions')}${t('error.promptContinuation')}`,
            options: ['重新生成', '我自己输入书名'],
            failedRequest: requestData,
          };
          setMessages(prev => [...prev, errorMessage]);
          setLastFailedRequest(requestData);
          return;
        }

        const aiMessage: Message = {
          type: 'ai',
          content: response.prompt || t('prompt.chooseTitle'),
          options: response.options,
          canRefine: true,
          step: 'title'
        };
        setMessages(prev => [...prev, aiMessage]);
        setCurrentStep('title');
        setLastFailedRequest(null);
      } else {
        await handleCustomInput(userInput);
      }
    } catch (error: unknown) {
      if (sessionVersion !== sessionVersionRef.current) return;
      console.error('发送消息失败:', error);
      const errMsg = error instanceof Error ? error.message : t('error.generateFailed');
      const axiosError = error as { response?: { data?: { detail?: string } } };
      message.error(axiosError.response?.data?.detail || errMsg);
    } finally {
      if (sessionVersion === sessionVersionRef.current) setLoading(false);
    }
  };

  const handleSelectOption = async (option: string, sourceMessage?: Message) => {
    const retryRequest = sourceMessage?.failedRequest || lastFailedRequest;
    if ((option === '重新生成' || option === '让AI重新生成') && retryRequest) {
      await handleRetry(retryRequest);
      return;
    }

    if (loading || refining) return;

    if (option === '我自己输入书名' || option === '我自己输入') {
      message.info(t('toast.selfInput'));
      return;
    }

    // 对于多选类型，不立即禁用选项
    if (currentStep === 'genre') {
      const newSelected = selectedOptions.includes(option)
        ? selectedOptions.filter(o => o !== option)
        : [...selectedOptions, option];
      setSelectedOptions(newSelected);
      return;
    }

    // 立即禁用当前消息的选项（单选场景）
    setMessages(prev => {
      const newMessages = [...prev];
      const lastAiMessageIndex = newMessages.map((m, i) => m.type === 'ai' && m.options ? i : -1).filter(i => i >= 0).pop();
      if (lastAiMessageIndex !== undefined && lastAiMessageIndex >= 0) {
        newMessages[lastAiMessageIndex] = {
          ...newMessages[lastAiMessageIndex],
          optionsDisabled: true
        };
      }
      return newMessages;
    });

    if (currentStep === 'perspective') {
      const userMessage: Message = {
        type: 'user',
        content: option,
      };
      setMessages(prev => [...prev, userMessage]);

      const updatedData = { ...wizardData, narrative_perspective: option };
      setWizardData(updatedData);

      // 询问大纲模式
      const aiMessage: Message = {
        type: 'ai',
        content: t('prompt.outlineMode'),
        options: ['📋 一对一模式', '📚 一对多模式']
      };
      setMessages(prev => [...prev, aiMessage]);
      setCurrentStep('outline_mode');
      return;
    }

    if (currentStep === 'outline_mode') {
      const userMessage: Message = {
        type: 'user',
        content: option,
      };
      setMessages(prev => [...prev, userMessage]);

      // 将选项转换为实际的模式值
      const modeValue: 'one-to-one' | 'one-to-many' =
        option === '📋 一对一模式' ? 'one-to-one' : 'one-to-many';

      const updatedData = {
        ...wizardData,
        outline_mode: modeValue,
        genre: wizardData.genre || []
      } as WizardData;
      setWizardData(updatedData);

      // 显示摘要
      const modeText = modeValue === 'one-to-one' ? t('mode.oneToOne') : t('mode.oneToMany');
      const summary = t('summary.block', {
        title: updatedData.title,
        description: updatedData.description,
        theme: updatedData.theme,
        genre: updatedData.genre.join('、'),
        perspective: updatedData.narrative_perspective,
        mode: modeText,
      });

      const aiMessage: Message = {
        type: 'ai',
        content: summary,
        options: ['✅ 确认创建', '🔄 重新开始']
      };
      setMessages(prev => [...prev, aiMessage]);
      setCurrentStep('confirm');
      return;
    }

    if (currentStep === 'confirm') {
      if (option === '✅ 确认创建') {
        const userMessage: Message = {
          type: 'user',
          content: t('aiMessage.confirmCreate'),
        };
        setMessages(prev => [...prev, userMessage]);

        const aiMessage: Message = {
          type: 'ai',
          content: t('aiMessage.confirmContent')
        };
        setMessages(prev => [...prev, aiMessage]);

        // 清除缓存（对话完成，进入生成阶段）
        clearCache();

        // 开始生成项目
        const data = wizardData as WizardData;
        const config: GenerationConfig = {
          title: data.title,
          description: data.description,
          theme: data.theme,
          genre: data.genre,
          narrative_perspective: data.narrative_perspective,
          target_words: 100000,
          chapter_count: 3,
          character_count: 5,
          outline_mode: data.outline_mode,
        };
        setGenerationConfig(config);
        setCurrentStep('generating');
        return;
      } else if (option === '🔄 重新开始') {
        handleRestart();
        return;
      }
    }

    const userMessage: Message = {
      type: 'user',
      content: option,
    };
    setMessages(prev => [...prev, userMessage]);
    const sessionVersion = sessionVersionRef.current;
    setLoading(true);

    try {
      const updatedData = { ...wizardData };
      if (currentStep === 'title') {
        updatedData.title = option;
      } else if (currentStep === 'description') {
        updatedData.description = option;
      } else if (currentStep === 'theme') {
        updatedData.theme = option;
      }
      setWizardData(updatedData);

      await generateNextStep(updatedData);
    } catch (error: unknown) {
      if (sessionVersion !== sessionVersionRef.current) return;
      console.error('选择选项失败:', error);
      const errMsg = error instanceof Error ? error.message : t('error.generateFailed');
      const axiosError = error as { response?: { data?: { detail?: string } } };
      message.error(axiosError.response?.data?.detail || errMsg);
    } finally {
      if (sessionVersion === sessionVersionRef.current) setLoading(false);
    }
  };

  const handleCustomInput = async (input: string) => {
    const sessionVersion = sessionVersionRef.current;
    setLoading(true);
    try {
      const updatedData = { ...wizardData };

      if (currentStep === 'title') {
        updatedData.title = input;
      } else if (currentStep === 'description') {
        updatedData.description = input;
      } else if (currentStep === 'theme') {
        updatedData.theme = input;
      } else if (currentStep === 'genre') {
        updatedData.genre = [input];
      } else if (currentStep === 'perspective') {
        updatedData.narrative_perspective = input;
        setWizardData(updatedData);
        
        // 直接进入大纲模式选择
        const aiMessage: Message = {
          type: 'ai',
          content: t('prompt.outlineMode'),
          options: ['📋 一对一模式', '📚 一对多模式']
        };
        setMessages(prev => [...prev, aiMessage]);
        setCurrentStep('outline_mode');
        setLoading(false);
        return;
      } else if (currentStep === 'outline_mode') {
        // 大纲模式不支持自定义输入
        message.warning(t('toast.selectOutlineMode'));
        setLoading(false);
        return;
      }

      setWizardData(updatedData);
      await generateNextStep(updatedData);
    } catch (error: unknown) {
      if (sessionVersion !== sessionVersionRef.current) return;
      console.error('处理自定义输入失败:', error);
      const errMsg = error instanceof Error ? error.message : t('error.processFailed');
      const axiosError = error as { response?: { data?: { detail?: string } } };
      message.error(axiosError.response?.data?.detail || errMsg);
    } finally {
      if (sessionVersion === sessionVersionRef.current) setLoading(false);
    }
  };

  const handleConfirmGenres = async () => {
    if (selectedOptions.length === 0) {
      message.warning(t('toast.selectGenre'));
      return;
    }

    // 禁用类型选择的选项
    setMessages(prev => {
      const newMessages = [...prev];
      const lastAiMessageIndex = newMessages.map((m, i) => m.type === 'ai' && m.options ? i : -1).filter(i => i >= 0).pop();
      if (lastAiMessageIndex !== undefined && lastAiMessageIndex >= 0) {
        newMessages[lastAiMessageIndex] = {
          ...newMessages[lastAiMessageIndex],
          optionsDisabled: true
        };
      }
      return newMessages;
    });

    const userMessage: Message = {
      type: 'user',
      content: selectedOptions.join('、'),
    };
    setMessages(prev => [...prev, userMessage]);

    const updatedData = { ...wizardData, genre: selectedOptions };
    setWizardData(updatedData);
    setSelectedOptions([]);

    setLoading(true);
    try {
      const aiMessage: Message = {
        type: 'ai',
        content: t('prompt.choosePerspective'),
        options: ['第一人称', '第三人称', '全知视角']
      };
      setMessages(prev => [...prev, aiMessage]);
      setCurrentStep('perspective');
    } finally {
      setLoading(false);
    }
  };

  const generateNextStep = async (data: Partial<WizardData>) => {
    const sessionVersion = sessionVersionRef.current;
    const currentIndex = stepOrder.indexOf(currentStep);
    const nextStep = stepOrder[currentIndex + 1];

    if (nextStep === 'perspective') {
      // genre 步骤完成后，进入 perspective
      const aiMessage: Message = {
        type: 'ai',
        content: t('prompt.choosePerspective'),
        options: ['第一人称', '第三人称', '全知视角']
      };
      setMessages(prev => [...prev, aiMessage]);
      setCurrentStep('perspective');
    } else if (nextStep === 'description') {
      const requestData = {
        step: 'description' as const,
        context: {
          initial_idea: initialIdea,
          title: data.title
        }
      };
      const response = await inspirationApi.generateOptions(requestData);

      if (sessionVersion !== sessionVersionRef.current) return;

      if (response.error || !response.options || response.options.length < 3) {
        const errorMessage: Message = {
          type: 'ai',
          content: response.error
            ? `${t('error.descError', { msg: response.error })}${t('error.promptContinuation')}`
            : `${t('error.invalidOptions')}${t('error.promptContinuation')}`,
          options: ['重新生成', '我自己输入'],
          failedRequest: requestData,
        };
        setMessages(prev => [...prev, errorMessage]);
        setLastFailedRequest(requestData);
        return;
      }

      const aiMessage: Message = {
        type: 'ai',
        content: response.prompt || t('prompt.chooseDesc'),
        options: response.options,
        canRefine: true,
        step: 'description'
      };
      setMessages(prev => [...prev, aiMessage]);
      setCurrentStep('description');
      setLastFailedRequest(null);

    } else if (nextStep === 'theme') {
      const requestData = {
        step: 'theme' as const,
        context: {
          initial_idea: initialIdea,
          title: data.title,
          description: data.description
        }
      };
      const response = await inspirationApi.generateOptions(requestData);

      if (sessionVersion !== sessionVersionRef.current) return;

      if (response.error || !response.options || response.options.length < 3) {
        const errorMessage: Message = {
          type: 'ai',
          content: response.error
            ? `${t('error.themeError', { msg: response.error })}${t('error.promptContinuation')}`
            : `${t('error.invalidOptions')}${t('error.promptContinuation')}`,
          options: ['重新生成', '我自己输入'],
          failedRequest: requestData,
        };
        setMessages(prev => [...prev, errorMessage]);
        setLastFailedRequest(requestData);
        return;
      }

      const aiMessage: Message = {
        type: 'ai',
        content: response.prompt || t('prompt.chooseTheme'),
        options: response.options,
        canRefine: true,
        step: 'theme'
      };
      setMessages(prev => [...prev, aiMessage]);
      setCurrentStep('theme');
      setLastFailedRequest(null);

    } else if (nextStep === 'genre') {
      const requestData = {
        step: 'genre' as const,
        context: {
          initial_idea: initialIdea,
          title: data.title,
          description: data.description,
          theme: data.theme
        }
      };
      const response = await inspirationApi.generateOptions(requestData);

      if (sessionVersion !== sessionVersionRef.current) return;

      if (response.error || !response.options || response.options.length < 3) {
        const errorMessage: Message = {
          type: 'ai',
          content: response.error
            ? `${t('error.genreError', { msg: response.error })}${t('error.promptContinuation')}`
            : `${t('error.invalidOptions')}${t('error.promptContinuation')}`,
          options: ['重新生成', '我自己输入'],
          isMultiSelect: false,
          failedRequest: requestData,
        };
        setMessages(prev => [...prev, errorMessage]);
        setLastFailedRequest(requestData);
        return;
      }

      const aiMessage: Message = {
        type: 'ai',
        content: response.prompt || t('prompt.chooseGenre'),
        options: response.options,
        isMultiSelect: true,
        canRefine: true,
        step: 'genre'
      };
      setMessages(prev => [...prev, aiMessage]);
      setCurrentStep('genre');
      setLastFailedRequest(null);
    }
  };

  const handleRestart = () => {
    sessionVersionRef.current += 1;
    // 清除缓存
    clearCache();
    localStorage.removeItem('inspiration_project_id');
    localStorage.removeItem('inspiration_generation_data');
    localStorage.removeItem('inspiration_current_step');

    setCurrentStep('idea');
    setMessages([
      {
        type: 'ai',
        content: t('greeting.restart'),
      }
    ]);
    setWizardData({});
    setInitialIdea('');
    setSelectedOptions([]);
    setLastFailedRequest(null);
    setFeedbackValue('');
    setShowFeedbackInput(null);
    setRefining(false);
    setGenerationConfig(null);
    setInputValue('');
    setLoading(false);
  };

  const handleBack = () => {
    navigate('/projects');
  };

  // 生成完成回调
  const handleComplete = (projectId: string) => {
    console.log('灵感模式项目创建完成:', projectId);
    // 确保清除缓存
    clearCache();
    setCurrentStep('complete');
  };

  // 返回对话界面
  const handleBackToChat = () => {
    clearCache();
    setCurrentStep('idea');
    setGenerationConfig(null);
    handleRestart();
  };

  // 渲染对话界面
  const renderChat = () => (
    <>
      <Card
        ref={chatContainerRef}
        style={{
          height: isMobile ? 'calc(100vh - 280px)' : 600,
          overflowY: 'auto',
          marginBottom: 16,
          boxShadow: `0 8px 24px color-mix(in srgb, ${token.colorTextBase} 20%, transparent)`,
          scrollBehavior: 'smooth'
        }}
      >
        <Space direction="vertical" style={{ width: '100%' }} size="large">
          {messages.map((msg, index) => (
            <div
              key={index}
              style={{
                display: 'flex',
                justifyContent: msg.type === 'ai' ? 'flex-start' : 'flex-end',
                alignItems: 'flex-start',
                animation: 'fadeInUp 0.5s ease-out',
                animationFillMode: 'both',
                animationDelay: `${index * 0.1}s`
              }}
            >
              <div style={{
                maxWidth: '80%',
                padding: '12px 16px',
                borderRadius: 12,
                background: msg.type === 'ai' ? token.colorBgContainer : token.colorPrimary,
                color: msg.type === 'ai' ? token.colorText : token.colorWhite,
                boxShadow: msg.type === 'ai'
                  ? `0 2px 10px color-mix(in srgb, ${token.colorTextBase} 12%, transparent)`
                  : `0 4px 14px color-mix(in srgb, ${token.colorPrimary} 30%, transparent)`,
              }}>
                <Paragraph
                  style={{
                    margin: 0,
                    color: msg.type === 'ai' ? token.colorText : token.colorWhite,
                    whiteSpace: 'pre-wrap'
                  }}
                >
                  {msg.content}
                </Paragraph>

                {msg.options && msg.options.length > 0 && (
                  <Space
                    direction="vertical"
                    style={{ width: '100%', marginTop: 12 }}
                    size="small"
                  >
                    {msg.options.map((option, optIndex) => (
                      <Card
                        key={optIndex}
                        hoverable={!msg.optionsDisabled}
                        size="small"
                        onClick={() => !msg.optionsDisabled && !loading && !refining && handleSelectOption(option, msg)}
                        style={{
                          cursor: msg.optionsDisabled || loading || refining ? 'not-allowed' : 'pointer',
                          border: msg.isMultiSelect && selectedOptions.includes(option)
                            ? `2px solid ${token.colorPrimary}`
                            : `1px solid ${token.colorBorder}`,
                          background: msg.optionsDisabled
                            ? token.colorBgLayout
                            : msg.isMultiSelect && selectedOptions.includes(option)
                              ? token.colorPrimaryBg
                              : token.colorBgContainer,
                          opacity: msg.optionsDisabled ? 0.6 : 1,
                          animation: 'floatIn 0.6s ease-out',
                          animationDelay: `${optIndex * 0.1}s`,
                          animationFillMode: 'both',
                          transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                        }}
                        onMouseEnter={(e) => {
                          if (!msg.optionsDisabled) {
                            e.currentTarget.style.transform = 'translateY(-2px) scale(1.02)';
                            e.currentTarget.style.boxShadow = `0 8px 22px color-mix(in srgb, ${token.colorTextBase} 14%, transparent)`;
                          }
                        }}
                        onMouseLeave={(e) => {
                          if (!msg.optionsDisabled) {
                            e.currentTarget.style.transform = 'translateY(0) scale(1)';
                            e.currentTarget.style.boxShadow = 'none';
                          }
                        }}
                      >
                        {option}
                      </Card>
                    ))}

                    {msg.isMultiSelect && (
                      <Button
                        type="primary"
                        block
                        onClick={handleConfirmGenres}
                        disabled={selectedOptions.length === 0}
                      >
                        {t('ui.confirmSelection', { num: selectedOptions.length })}
                      </Button>
                    )}

                    {/* 反馈优化区域 - 新增 */}
                    {msg.canRefine && !msg.optionsDisabled && !msg.isMultiSelect && (
                      <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px dashed ${token.colorBorder}` }}>
                        {showFeedbackInput === index ? (
                          <Space direction="vertical" style={{ width: '100%' }} size="small">
                            <TextArea
                              value={feedbackValue}
                              onChange={(e) => setFeedbackValue(e.target.value)}
                               placeholder={t('ui.refinePlaceholder')}
                              autoSize={{ minRows: 2, maxRows: 3 }}
                              disabled={refining}
                              onPressEnter={(e) => {
                                if (!e.shiftKey && feedbackValue.trim()) {
                                  e.preventDefault();
                                  handleRefineOptions(index, feedbackValue);
                                }
                              }}
                            />
                            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
                              <Button
                                size="small"
                                onClick={() => {
                                  setShowFeedbackInput(null);
                                  setFeedbackValue('');
                                }}
                                disabled={refining}
                              >
                                {t('ui.cancel')}
                              </Button>
                              <Button
                                type="primary"
                                size="small"
                                onClick={() => handleRefineOptions(index, feedbackValue)}
                                loading={refining}
                                disabled={!feedbackValue.trim()}
                              >
                                {t('ui.refineGenerate')}
                              </Button>
                            </Space>
                          </Space>
                        ) : (
                          <Button
                            type="link"
                            size="small"
                            onClick={() => setShowFeedbackInput(index)}
                            style={{ padding: 0, height: 'auto' }}
                          >
                            {t('ui.feedbackPrompt')}
                          </Button>
                        )}
                      </div>
                    )}
                  </Space>
                )}
              </div>
            </div>
          ))}

          {(loading || refining) && (
            <div style={{
              textAlign: 'center',
              padding: 20,
              animation: 'fadeIn 0.3s ease-in'
            }}>
              <Spin tip={refining ? t("ui.spinRefining") : t("ui.spinThinking")} />
            </div>
          )}

          <div ref={messagesEndRef} />
        </Space>
      </Card>

      <Card
        style={{ boxShadow: `0 4px 12px color-mix(in srgb, ${token.colorTextBase} 14%, transparent)` }}
        styles={{ body: { padding: 12 } }}
      >
        <Space.Compact style={{ width: '100%' }}>
          <TextArea
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            placeholder={
              currentStep === 'idea'
                ? t('prompt.placeholderIdea')
                : t('prompt.placeholderOther')
            }
            autoSize={{ minRows: 2, maxRows: 4 }}
            onPressEnter={(e) => {
              if (!e.shiftKey) {
                e.preventDefault();
                handleSendMessage();
              }
            }}
            disabled={loading}
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={handleSendMessage}
            loading={loading}
            style={{ height: 'auto' }}
          >
            {t('ui.send')}
          </Button>
        </Space.Compact>
        <Text type="secondary" style={{ fontSize: 12, marginTop: 8, display: 'block' }}>
          {t('ui.tip')}
        </Text>
      </Card>
    </>
  );

  return (
    <div style={{
      minHeight: '100dvh',
      background: token.colorBgBase,
    }}>
      {contextHolder}
      <style>
        {`
          @keyframes fadeInUp {
            from {
              opacity: 0;
              transform: translateY(20px);
            }
            to {
              opacity: 1;
              transform: translateY(0);
            }
          }
          
          @keyframes floatIn {
            0% {
              opacity: 0;
              transform: translateY(10px) scale(0.95);
            }
            60% {
              transform: translateY(-5px) scale(1.02);
            }
            100% {
              opacity: 1;
              transform: translateY(0) scale(1);
            }
          }
          
          @keyframes fadeIn {
            from {
              opacity: 0;
            }
            to {
              opacity: 1;
            }
          }
        `}
      </style>

      {/* 顶部标题栏 - 固定不滚动 */}
      <div style={{
        position: 'sticky',
        top: 0,
        zIndex: 100,
        background: token.colorPrimary,
        boxShadow: `0 6px 20px color-mix(in srgb, ${token.colorPrimary} 30%, transparent)`,
      }}>
        <div style={{
          maxWidth: 1200,
          margin: '0 auto',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: isMobile ? '12px 16px' : '16px 24px',
        }}>
          <Button
            icon={<ArrowLeftOutlined />}
            onClick={handleBack}
            size={isMobile ? 'middle' : 'large'}
            style={{
              background: `color-mix(in srgb, ${token.colorWhite} 20%, transparent)`,
              borderColor: `color-mix(in srgb, ${token.colorWhite} 30%, transparent)`,
              color: token.colorWhite,
            }}
          >
            {isMobile ? t('ui.return') : t('ui.returnHome')}
          </Button>

          <div style={{ textAlign: 'center' }}>
            <Title
              level={isMobile ? 4 : 2}
              style={{
                margin: 0,
                color: token.colorWhite,
                textShadow: '0 2px 4px color-mix(in srgb, var(--ant-color-black) 18%, transparent)',
                lineHeight: 1.2
              }}
            >
              {t('ui.title')}
            </Title>
          </div>

          {/* 重新开始按钮 - 只在对话进行中显示 */}
          {currentStep !== 'idea' && currentStep !== 'generating' && currentStep !== 'complete' ? (
            <Button
              icon={<ReloadOutlined />}
              onClick={() => {
                modal.confirm({
                  title: t('confirm.title'),
                  content: t('confirm.content'),
                  okText: t('confirm.ok'),
                  cancelText: t('confirm.cancel'),
                  centered: true,
                  okButtonProps: { danger: true },
                  onOk: () => {
                    handleRestart();
                  },
                });
              }}
              size={isMobile ? 'middle' : 'large'}
              style={{
                background: `color-mix(in srgb, ${token.colorWhite} 20%, transparent)`,
                borderColor: `color-mix(in srgb, ${token.colorWhite} 30%, transparent)`,
                color: token.colorWhite,
              }}
            >
              {isMobile ? t('ui.restartShort') : t('ui.restart')}
            </Button>
          ) : (
            <div style={{ width: isMobile ? 60 : 120 }}></div>
          )}
        </div>
      </div>

      <div style={{
        maxWidth: 800,
        margin: '0 auto',
        padding: isMobile ? '16px 12px' : '24px 24px',
      }}>
        {(currentStep === 'idea' || currentStep === 'title' || currentStep === 'description' ||
          currentStep === 'theme' || currentStep === 'genre' || currentStep === 'perspective' ||
          currentStep === 'outline_mode' || currentStep === 'confirm') && renderChat()}
        {(currentStep === 'generating' || currentStep === 'complete') && generationConfig && (
          <AIProjectGenerator
            config={generationConfig}
            storagePrefix="inspiration"
            onComplete={handleComplete}
            onBack={handleBackToChat}
            isMobile={isMobile}
          />
        )}
      </div>
    </div>
  );
};

export default Inspiration;
