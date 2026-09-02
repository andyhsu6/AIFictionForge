import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { App, Card, Button, Space, Typography, Progress, theme } from 'antd';
import { CheckCircleOutlined, LoadingOutlined } from '@ant-design/icons';
import { wizardStreamApi } from '../services/api';
import type { ApiError } from '../types';

const { Title, Paragraph, Text } = Typography;

export interface GenerationConfig {
  title: string;
  description: string;
  theme: string;
  genre: string | string[];
  narrative_perspective: string;
  target_words: number;
  chapter_count: number;
  character_count: number;
  outline_mode?: 'one-to-one' | 'one-to-many';  // 大纲章节模式
}

interface AIProjectGeneratorProps {
  config: GenerationConfig;
  storagePrefix: 'wizard' | 'inspiration';
  onComplete: (projectId: string) => void;
  onBack?: () => void;
  isMobile?: boolean;
  resumeProjectId?: string;
}

type GenerationStep = 'pending' | 'processing' | 'completed' | 'error';

interface GenerationSteps {
  worldBuilding: GenerationStep;
  careers: GenerationStep;
  characters: GenerationStep;
  outline: GenerationStep;
}

interface WorldBuildingResult {
  project_id: string;
  time_period: string;
  location: string;
  atmosphere: string;
  rules: string;
}

const isAbortError = (error: unknown): boolean =>
  typeof error === 'object'
  && error !== null
  && 'name' in error
  && error.name === 'AbortError';

export const AIProjectGenerator: React.FC<AIProjectGeneratorProps> = ({
  config,
  storagePrefix,
  onComplete,
  onBack,
  isMobile = false,
  resumeProjectId
}) => {
  const { message, modal } = App.useApp();
  const navigate = useNavigate();
  const { t } = useTranslation('aiProjectGenerator');
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) =>
    `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;

  // 状态管理
  const [loading, setLoading] = useState(false);
  const [projectId, setProjectId] = useState<string>('');

  // SSE流式进度状态
  const [progress, setProgress] = useState(0);
  const [progressMessage, setProgressMessage] = useState('');
  const [errorDetails, setErrorDetails] = useState<string>('');
  const [generationSteps, setGenerationSteps] = useState<GenerationSteps>({
    worldBuilding: 'pending',
    careers: 'pending',
    characters: 'pending',
    outline: 'pending'
  });

  // 保存生成数据，用于重试
  const [generationData, setGenerationData] = useState<GenerationConfig | null>(null);
  // 保存世界观生成结果，用于后续步骤
  const [worldBuildingResult, setWorldBuildingResult] = useState<WorldBuildingResult | null>(null);

  // LocalStorage 键名
  const storageKeys = {
    projectId: `${storagePrefix}_project_id`,
    generationData: `${storagePrefix}_generation_data`,
    currentStep: `${storagePrefix}_current_step`
  };

  // 保存进度到localStorage
  const saveProgress = (projectId: string, data: GenerationConfig, step: string) => {
    try {
      localStorage.setItem(storageKeys.projectId, projectId);
      localStorage.setItem(storageKeys.generationData, JSON.stringify(data));
      localStorage.setItem(storageKeys.currentStep, step);
    } catch (error) {
      console.error('保存进度失败:', error);
    }
  };

  // 清理localStorage
  const clearStorage = () => {
    localStorage.removeItem(storageKeys.projectId);
    localStorage.removeItem(storageKeys.generationData);
    localStorage.removeItem(storageKeys.currentStep);
  };

  const handleRestartGeneration = () => {
    modal.confirm({
      title: t('confirmRestartTitle'),
      content: t('confirmRestartContent'),
      okText: t('restart'),
      cancelText: t('cancel'),
      centered: true,
      okButtonProps: { danger: true },
      onOk: () => {
        clearStorage();
        onBack?.();
      },
    });
  };

  // 使用配置内容作为依赖，避免父组件创建等价的新对象时重新启动生成流程。
  const generationKey = JSON.stringify([
    config.title,
    config.description,
    config.theme,
    config.genre,
    config.narrative_perspective,
    config.target_words,
    config.chapter_count,
    config.character_count,
    config.outline_mode,
  ]);

  // 开始自动化生成流程
  useEffect(() => {
    const controller = new AbortController();

    // 延迟到下一轮事件循环：StrictMode 会立即清理首次探测 Effect，
    // 因此探测执行不会真正发出生成请求，第二次正式 Effect 才会启动流程。
    const startTimer = window.setTimeout(() => {
      if (controller.signal.aborted) return;

      if (resumeProjectId) {
        // 恢复生成模式
        void handleResumeGenerate(config, resumeProjectId, controller.signal);
      } else {
        // 新建项目模式
        void handleAutoGenerate(config, controller.signal);
      }
    }, 0);

    return () => {
      window.clearTimeout(startTimer);
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generationKey, resumeProjectId]);

  // 恢复未完成项目的生成
  const handleResumeGenerate = async (
    data: GenerationConfig,
    projectIdParam: string,
    signal?: AbortSignal,
  ) => {
    try {
      setLoading(true);
      setProgress(0);
      setProgressMessage(t('checkingStatus'));
      setErrorDetails('');
      setGenerationData(data);
      setProjectId(projectIdParam);

      // 获取项目信息,判断当前完成到哪一步
      const response = await fetch(`/api/projects/${projectIdParam}`, {
        credentials: 'include',
        signal,
      });
      if (!response.ok) {
        throw new Error(t('fetchProjectFailed'));
      }
      const project = await response.json();
      const wizardStep = project.wizard_step || 0;

      // 根据wizard_step判断从哪里继续
      // wizard_step: 0=未开始, 1=世界观已完成, 2=职业体系已完成, 3=角色已完成, 4=大纲已完成
      // 获取世界观数据（用于后续步骤）
      const worldResult = {
        project_id: projectIdParam,
        time_period: project.world_time_period || '',
        location: project.world_location || '',
        atmosphere: project.world_atmosphere || '',
        rules: project.world_rules || ''
      };

      if (wizardStep === 0) {
        // 从世界观开始
        message.info(t('resumeFromWorld'));
        setGenerationSteps({ worldBuilding: 'processing', careers: 'pending', characters: 'pending', outline: 'pending' });
        await resumeFromWorldBuilding(data, signal);
      } else if (wizardStep === 1) {
        // 世界观已完成，从职业体系开始
        message.info(t('resumeFromCareers'));
        setGenerationSteps({ worldBuilding: 'completed', careers: 'processing', characters: 'pending', outline: 'pending' });
        setWorldBuildingResult(worldResult);
        setProgress(20);
        await resumeFromCareers(data, worldResult, signal);
      } else if (wizardStep === 2) {
        // 职业体系已完成，从角色开始
        message.info(t('resumeFromCharacters'));
        setGenerationSteps({ worldBuilding: 'completed', careers: 'completed', characters: 'processing', outline: 'pending' });
        setWorldBuildingResult(worldResult);
        setProgress(40);
        await resumeFromCharacters(data, worldResult, signal);
      } else if (wizardStep === 3) {
        // 角色已完成，从大纲开始
        message.info(t('resumeFromOutline'));
        setGenerationSteps({ worldBuilding: 'completed', careers: 'completed', characters: 'completed', outline: 'processing' });
        setProgress(70);
        await resumeFromOutline(data, projectIdParam, signal);
      } else {
        // 已全部完成
        message.success(t('resumeComplete'));
        setProgress(100);
        onComplete(projectIdParam);
        setTimeout(() => {
          navigate(`/project/${projectIdParam}`);
        }, 1000);
      }
    } catch (error) {
      if (isAbortError(error)) return;

      const apiError = error as ApiError;
      const errorMsg = apiError.response?.data?.detail || apiError.message || t('unknownError');
      console.error('恢复生成失败:', errorMsg);
      setErrorDetails(errorMsg);
      message.error(t('resumeFailed', { error: errorMsg }));
      setLoading(false);
    }
  };

  // 恢复:从世界观步骤开始
  const resumeFromWorldBuilding = async (data: GenerationConfig, signal?: AbortSignal) => {
    const genreString = Array.isArray(data.genre) ? data.genre.join('、') : data.genre;

    const worldResult = await wizardStreamApi.generateWorldBuildingStream(
      {
        title: data.title,
        description: data.description,
        theme: data.theme,
        genre: genreString,
        narrative_perspective: data.narrative_perspective,
        target_words: data.target_words,
        chapter_count: data.chapter_count,
        character_count: data.character_count,
        outline_mode: data.outline_mode || 'one-to-many',  // 传递大纲模式
      },
      {
        signal,
        onProgress: (msg, prog) => {
          // 直接使用后端返回的进度值
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: (result) => {
          setWorldBuildingResult(result);
          setGenerationSteps(prev => ({ ...prev, worldBuilding: 'completed' }));
        },
        onError: (error) => {
          console.error('世界观生成失败:', error);
          setErrorDetails(t('worldError', { error }));
          setGenerationSteps(prev => ({ ...prev, worldBuilding: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('世界观生成完成');
        }
      }
    );

    await resumeFromCareers(data, worldResult, signal);
  };

  // 恢复:从职业体系步骤继续
  const resumeFromCareers = async (
    data: GenerationConfig,
    worldResult: WorldBuildingResult,
    signal?: AbortSignal,
  ) => {
    const pid = projectId || worldResult.project_id;

    setGenerationSteps(prev => ({ ...prev, careers: 'processing' }));
    setProgressMessage(t('generatingCareers'));

    await wizardStreamApi.generateCareerSystemStream(
      {
        project_id: pid,
      },
      {
        signal,
        onProgress: (msg, prog) => {
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: (result) => {
          console.log(`成功生成职业体系：主职业${result.main_careers_count}个，副职业${result.sub_careers_count}个`);
          setGenerationSteps(prev => ({ ...prev, careers: 'completed' }));
        },
        onError: (error) => {
          console.error('职业体系生成失败:', error);
          setErrorDetails(t('careersError', { error }));
          setGenerationSteps(prev => ({ ...prev, careers: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('职业体系生成完成');
        }
      }
    );

    await resumeFromCharacters(data, worldResult, signal);
  };

  // 恢复:从角色步骤继续
  const resumeFromCharacters = async (
    data: GenerationConfig,
    worldResult: WorldBuildingResult,
    signal?: AbortSignal,
  ) => {
    const genreString = Array.isArray(data.genre) ? data.genre.join('、') : data.genre;
    const pid = projectId || worldResult.project_id;

    setGenerationSteps(prev => ({ ...prev, characters: 'processing' }));
    setProgressMessage(t('generatingCharacters'));

    await wizardStreamApi.generateCharactersStream(
      {
        project_id: pid,
        count: data.character_count,
        world_context: {
          time_period: worldResult.time_period || '',
          location: worldResult.location || '',
          atmosphere: worldResult.atmosphere || '',
          rules: worldResult.rules || '',
        },
        theme: data.theme,
        genre: genreString,
      },
      {
        signal,
        onProgress: (msg, prog) => {
          // 直接使用后端返回的进度值
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: (result) => {
          console.log(`成功生成${result.characters?.length || 0}个角色`);
          setGenerationSteps(prev => ({ ...prev, characters: 'completed' }));
        },
        onError: (error) => {
          console.error('角色生成失败:', error);
          setErrorDetails(t('charactersError', { error }));
          setGenerationSteps(prev => ({ ...prev, characters: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('角色生成完成');
        }
      }
    );

    await resumeFromOutline(data, pid, signal);
  };

  // 恢复:从大纲步骤继续
  const resumeFromOutline = async (
    data: GenerationConfig,
    pid: string,
    signal?: AbortSignal,
  ) => {
    setGenerationSteps(prev => ({ ...prev, outline: 'processing' }));
    setProgressMessage(t('generatingOutline'));

    await wizardStreamApi.generateCompleteOutlineStream(
      {
        project_id: pid,
        chapter_count: data.chapter_count,
        narrative_perspective: data.narrative_perspective,
        target_words: data.target_words,
      },
      {
        signal,
        onProgress: (msg, prog) => {
          // 直接使用后端返回的进度值
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: () => {
          console.log('大纲生成完成');
          setGenerationSteps(prev => ({ ...prev, outline: 'completed' }));
        },
        onError: (error) => {
          console.error('大纲生成失败:', error);
          setErrorDetails(t('outlineError', { error }));
          setGenerationSteps(prev => ({ ...prev, outline: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('大纲生成完成');
        }
      }
    );

    // 全部完成
    setProgress(100);
    setProgressMessage(t('projectComplete'));
    message.success(t('projectSuccess'));
    clearStorage();
    setLoading(false);

    onComplete(pid);
    setTimeout(() => {
      navigate(`/project/${pid}`);
    }, 1000);
  };

  // 自动化生成流程
  const handleAutoGenerate = async (data: GenerationConfig, signal?: AbortSignal) => {
    try {
      setLoading(true);
      setProgress(0);
      setProgressMessage(t('creatingProject'));
      setErrorDetails('');
      setGenerationData(data);
      saveProgress('', data, 'generating');

      const genreString = Array.isArray(data.genre) ? data.genre.join('、') : data.genre;

      // 步骤1: 生成世界观并创建项目
      setGenerationSteps(prev => ({ ...prev, worldBuilding: 'processing' }));
      setProgressMessage(t('generatingWorld'));

      const worldResult = await wizardStreamApi.generateWorldBuildingStream(
        {
          title: data.title,
          description: data.description,
          theme: data.theme,
          genre: genreString,
          narrative_perspective: data.narrative_perspective,
          target_words: data.target_words,
          chapter_count: data.chapter_count,
          character_count: data.character_count,
          outline_mode: data.outline_mode || 'one-to-many',  // 传递大纲模式
        },
        {
          signal,
          onProgress: (msg, prog) => {
            // 直接使用后端返回的进度值
            setProgress(prog);
            setProgressMessage(msg);
          },
          onResult: (result) => {
            setProjectId(result.project_id);
            setWorldBuildingResult(result);
            setGenerationSteps(prev => ({ ...prev, worldBuilding: 'completed' }));
          },
          onError: (error) => {
            console.error('世界观生成失败:', error);
            setErrorDetails(t('worldError', { error }));
            setGenerationSteps(prev => ({ ...prev, worldBuilding: 'error' }));
            setLoading(false);
            throw new Error(error);
          },
          onComplete: () => {
            console.log('世界观生成完成');
          }
        }
      );

      if (!worldResult?.project_id) {
        throw new Error(t('projectIdMissing'));
      }

      const createdProjectId = worldResult.project_id;
      setProjectId(createdProjectId);
      setWorldBuildingResult(worldResult);
      saveProgress(createdProjectId, data, 'generating');

      // 步骤2: 生成职业体系
      setGenerationSteps(prev => ({ ...prev, careers: 'processing' }));
      setProgressMessage(t('generatingCareers'));

      await wizardStreamApi.generateCareerSystemStream(
        {
          project_id: createdProjectId,
        },
        {
          signal,
          onProgress: (msg, prog) => {
            setProgress(prog);
            setProgressMessage(msg);
          },
          onResult: (result) => {
            console.log(`成功生成职业体系：主职业${result.main_careers_count}个，副职业${result.sub_careers_count}个`);
            setGenerationSteps(prev => ({ ...prev, careers: 'completed' }));
          },
          onError: (error) => {
            console.error('职业体系生成失败:', error);
            setErrorDetails(t('careersError', { error }));
            setGenerationSteps(prev => ({ ...prev, careers: 'error' }));
            setLoading(false);
            throw new Error(error);
          },
          onComplete: () => {
            console.log('职业体系生成完成');
          }
        }
      );

      // 步骤3: 生成角色
      setGenerationSteps(prev => ({ ...prev, characters: 'processing' }));
      setProgressMessage(t('generatingCharacters'));

      await wizardStreamApi.generateCharactersStream(
        {
          project_id: createdProjectId,
          count: data.character_count,
          world_context: {
            time_period: worldResult.time_period || '',
            location: worldResult.location || '',
            atmosphere: worldResult.atmosphere || '',
            rules: worldResult.rules || '',
          },
          theme: data.theme,
          genre: genreString,
        },
        {
          signal,
          onProgress: (msg, prog) => {
            // 直接使用后端返回的进度值
            setProgress(prog);
            setProgressMessage(msg);
          },
          onResult: (result) => {
            console.log(`成功生成${result.characters?.length || 0}个角色`);
            setGenerationSteps(prev => ({ ...prev, characters: 'completed' }));
          },
          onError: (error) => {
            console.error('角色生成失败:', error);
            setErrorDetails(t('charactersError', { error }));
            setGenerationSteps(prev => ({ ...prev, characters: 'error' }));
            setLoading(false);
            throw new Error(error);
          },
          onComplete: () => {
            console.log('角色生成完成');
          }
        }
      );

      // 步骤3: 生成大纲
      setGenerationSteps(prev => ({ ...prev, outline: 'processing' }));
      setProgressMessage(t('generatingOutline'));

      await wizardStreamApi.generateCompleteOutlineStream(
        {
          project_id: createdProjectId,
          chapter_count: data.chapter_count,
          narrative_perspective: data.narrative_perspective,
          target_words: data.target_words,
        },
        {
          signal,
          onProgress: (msg, prog) => {
            // 直接使用后端返回的进度值
            setProgress(prog);
            setProgressMessage(msg);
          },
          onResult: () => {
            console.log('大纲生成完成');
            setGenerationSteps(prev => ({ ...prev, outline: 'completed' }));
          },
          onError: (error) => {
            console.error('大纲生成失败:', error);
            setErrorDetails(t('outlineError', { error }));
            setGenerationSteps(prev => ({ ...prev, outline: 'error' }));
            setLoading(false);
            throw new Error(error);
          },
          onComplete: () => {
            console.log('大纲生成完成');
          }
        }
      );

      // 全部完成 - 自动跳转到项目详情页
      setProgress(100);
      setProgressMessage(t('projectComplete'));
      message.success(t('projectSuccess'));
      clearStorage();

      // 调用完成回调
      onComplete(createdProjectId);

      // 延迟1秒后自动跳转到项目详情页
      setTimeout(() => {
        navigate(`/project/${createdProjectId}`);
      }, 1000);

    } catch (error) {
      if (isAbortError(error)) return;

      const apiError = error as ApiError;
      const errorMsg = apiError.response?.data?.detail || apiError.message || t('unknownError');
      console.error('创建项目失败:', errorMsg);
      setErrorDetails(errorMsg);
      message.error(t('createFailed', { error: errorMsg }));
      setLoading(false);
    }
  };

  // 智能重试：从失败的步骤继续生成
  const handleSmartRetry = async () => {
    if (!generationData) {
      message.warning(t('missingData'));
      return;
    }

    setLoading(true);
    setErrorDetails('');

    try {
      if (generationSteps.worldBuilding === 'error') {
        message.info(t('retryFromWorldInfo'));
        await retryFromWorldBuilding();
      } else if (generationSteps.careers === 'error') {
        message.info(t('retryFromCareersInfo'));
        await retryFromCareers();
      } else if (generationSteps.characters === 'error') {
        message.info(t('retryFromCharactersInfo'));
        await retryFromCharacters();
      } else if (generationSteps.outline === 'error') {
        message.info(t('retryFromOutlineInfo'));
        await retryFromOutline();
      }
    } catch (error) {
      console.error('智能重试失败:', error);
      const errorMessage = error instanceof Error ? error.message : t('unknownError');
      message.error(t('retryFailed', { error: errorMessage }));
      setLoading(false);
    }
  };

  // 从世界观步骤重新开始
  const retryFromWorldBuilding = async () => {
    if (!generationData) return;

    setGenerationSteps(prev => ({ ...prev, worldBuilding: 'processing' }));
    setProgressMessage(t('regeneratingWorld'));

    const genreString = Array.isArray(generationData.genre) ? generationData.genre.join('、') : generationData.genre;

    const worldResult = await wizardStreamApi.generateWorldBuildingStream(
      {
        title: generationData.title,
        description: generationData.description,
        theme: generationData.theme,
        genre: genreString,
        narrative_perspective: generationData.narrative_perspective,
        target_words: generationData.target_words,
        chapter_count: generationData.chapter_count,
        character_count: generationData.character_count,
        outline_mode: generationData.outline_mode || 'one-to-many',  // 传递大纲模式
      },
      {
        onProgress: (msg, prog) => {
          // 直接使用后端返回的进度值
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: (result) => {
          setProjectId(result.project_id);
          setWorldBuildingResult(result);
          setGenerationSteps(prev => ({ ...prev, worldBuilding: 'completed' }));
        },
        onError: (error) => {
          console.error('世界观生成失败:', error);
          setErrorDetails(t('worldError', { error }));
          setGenerationSteps(prev => ({ ...prev, worldBuilding: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('世界观重新生成完成');
        }
      }
    );

    if (!worldResult?.project_id) {
      throw new Error(t('projectIdMissing'));
    }

    await continueFromCareers(worldResult);
  };

  // 从职业体系步骤继续
  const retryFromCareers = async () => {
    if (!worldBuildingResult) {
      message.warning(t('missingDataCareers'));
      setLoading(false);
      return;
    }

    const pid = worldBuildingResult.project_id || projectId;
    if (!pid) {
      message.warning(t('missingProjectId'));
      setLoading(false);
      return;
    }

    setGenerationSteps(prev => ({ ...prev, careers: 'processing' }));
    setProgressMessage(t('regeneratingCareers'));

    await wizardStreamApi.generateCareerSystemStream(
      {
        project_id: pid,
      },
      {
        onProgress: (msg, prog) => {
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: (result) => {
          console.log(`成功生成职业体系：主职业${result.main_careers_count}个，副职业${result.sub_careers_count}个`);
          setGenerationSteps(prev => ({ ...prev, careers: 'completed' }));
        },
        onError: (error) => {
          console.error('职业体系生成失败:', error);
          setErrorDetails(t('careersError', { error }));
          setGenerationSteps(prev => ({ ...prev, careers: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('职业体系重新生成完成');
        }
      }
    );

    await continueFromCharacters(worldBuildingResult);
  };

  // 从角色步骤继续
  const retryFromCharacters = async () => {
    if (!generationData || !worldBuildingResult) {
      message.warning(t('missingDataCharacters'));
      setLoading(false);
      return;
    }

    // 优先使用 worldBuildingResult 中的 project_id，因为重试可能创建了新项目
    const pid = worldBuildingResult.project_id || projectId;
    if (!pid) {
      message.warning(t('missingProjectIdCharacters'));
      setLoading(false);
      return;
    }

    setGenerationSteps(prev => ({ ...prev, characters: 'processing' }));
    setProgressMessage(t('regeneratingCharacters'));

    const genreString = Array.isArray(generationData.genre) ? generationData.genre.join('、') : generationData.genre;

    await wizardStreamApi.generateCharactersStream(
      {
        project_id: pid,
        count: generationData.character_count,
        world_context: {
          time_period: worldBuildingResult.time_period || '',
          location: worldBuildingResult.location || '',
          atmosphere: worldBuildingResult.atmosphere || '',
          rules: worldBuildingResult.rules || '',
        },
        theme: generationData.theme,
        genre: genreString,
      },
      {
        onProgress: (msg, prog) => {
          // 直接使用后端返回的进度值
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: (result) => {
          console.log(`成功生成${result.characters?.length || 0}个角色`);
          setGenerationSteps(prev => ({ ...prev, characters: 'completed' }));
        },
        onError: (error) => {
          console.error('角色生成失败:', error);
          setErrorDetails(t('charactersError', { error }));
          setGenerationSteps(prev => ({ ...prev, characters: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('角色重新生成完成');
        }
      }
    );

    await continueFromOutline(pid);
  };

  // 从大纲步骤继续
  const retryFromOutline = async () => {
    if (!generationData) {
      message.warning(t('missingDataOutline'));
      setLoading(false);
      return;
    }

    // 优先使用 worldBuildingResult 中的 project_id，fallback 到状态中的 projectId
    const pid = (worldBuildingResult?.project_id) || projectId;
    if (!pid) {
      message.warning(t('missingProjectIdOutline'));
      setLoading(false);
      return;
    }

    setGenerationSteps(prev => ({ ...prev, outline: 'processing' }));
    setProgressMessage(t('regeneratingOutline'));

    await wizardStreamApi.generateCompleteOutlineStream(
      {
        project_id: pid,
        chapter_count: generationData.chapter_count,
        narrative_perspective: generationData.narrative_perspective,
        target_words: generationData.target_words,
      },
      {
        onProgress: (msg, prog) => {
          // 直接使用后端返回的进度值
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: () => {
          console.log('大纲生成完成');
          setGenerationSteps(prev => ({ ...prev, outline: 'completed' }));
        },
        onError: (error) => {
          console.error('大纲生成失败:', error);
          setErrorDetails(t('outlineError', { error }));
          setGenerationSteps(prev => ({ ...prev, outline: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('大纲重新生成完成');
        }
      }
    );

    setProgress(100);
    setProgressMessage(t('projectComplete'));
    message.success(t('projectSuccess'));
    setLoading(false);

    // 调用完成回调
    if (pid) {
      onComplete(pid);

      // 延迟1秒后自动跳转到项目详情页
      setTimeout(() => {
        navigate(`/project/${pid}`);
      }, 1000);
    }
  };

  // 从职业体系步骤开始的完整流程
  const continueFromCareers = async (worldResult: WorldBuildingResult) => {
    if (!generationData || !worldResult?.project_id) return;

    const pid = worldResult.project_id;

    setGenerationSteps(prev => ({ ...prev, careers: 'processing' }));
    setProgressMessage(t('generatingCareers'));

    await wizardStreamApi.generateCareerSystemStream(
      {
        project_id: pid,
      },
      {
        onProgress: (msg, prog) => {
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: (result) => {
          console.log(`成功生成职业体系：主职业${result.main_careers_count}个，副职业${result.sub_careers_count}个`);
          setGenerationSteps(prev => ({ ...prev, careers: 'completed' }));
        },
        onError: (error) => {
          console.error('职业体系生成失败:', error);
          setErrorDetails(t('careersError', { error }));
          setGenerationSteps(prev => ({ ...prev, careers: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('职业体系生成完成');
        }
      }
    );

    await continueFromCharacters(worldResult);
  };

  // 从角色步骤开始的完整流程
  const continueFromCharacters = async (worldResult: WorldBuildingResult) => {
    if (!generationData || !worldResult?.project_id) return;

    const pid = worldResult.project_id;
    const genreString = Array.isArray(generationData.genre) ? generationData.genre.join('、') : generationData.genre;

    setGenerationSteps(prev => ({ ...prev, characters: 'processing' }));
    setProgressMessage(t('generatingCharacters'));

    await wizardStreamApi.generateCharactersStream(
      {
        project_id: pid,
        count: generationData.character_count,
        world_context: {
          time_period: worldResult.time_period || '',
          location: worldResult.location || '',
          atmosphere: worldResult.atmosphere || '',
          rules: worldResult.rules || '',
        },
        theme: generationData.theme,
        genre: genreString,
      },
      {
        onProgress: (msg, prog) => {
          // 直接使用后端返回的进度值
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: (result) => {
          console.log(`成功生成${result.characters?.length || 0}个角色`);
          setGenerationSteps(prev => ({ ...prev, characters: 'completed' }));
        },
        onError: (error) => {
          console.error('角色生成失败:', error);
          setErrorDetails(t('charactersError', { error }));
          setGenerationSteps(prev => ({ ...prev, characters: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('角色生成完成');
        }
      }
    );

    await continueFromOutline(pid);
  };

  // 从大纲步骤开始的完整流程
  const continueFromOutline = async (pid: string) => {
    if (!generationData || !pid) return;

    setGenerationSteps(prev => ({ ...prev, outline: 'processing' }));
    setProgressMessage(t('generatingOutline'));

    await wizardStreamApi.generateCompleteOutlineStream(
      {
        project_id: pid,
        chapter_count: generationData.chapter_count,
        narrative_perspective: generationData.narrative_perspective,
        target_words: generationData.target_words,
      },
      {
        onProgress: (msg, prog) => {
          // 直接使用后端返回的进度值
          setProgress(prog);
          setProgressMessage(msg);
        },
        onResult: () => {
          console.log('大纲生成完成');
          setGenerationSteps(prev => ({ ...prev, outline: 'completed' }));
        },
        onError: (error) => {
          console.error('大纲生成失败:', error);
          setErrorDetails(t('outlineError', { error }));
          setGenerationSteps(prev => ({ ...prev, outline: 'error' }));
          setLoading(false);
          throw new Error(error);
        },
        onComplete: () => {
          console.log('大纲生成完成');
        }
      }
    );

    setProgress(100);
    setProgressMessage(t('projectComplete'));
    message.success(t('projectSuccess'));
    setLoading(false);

    // 调用完成回调
    if (pid) {
      onComplete(pid);

      // 延迟1秒后自动跳转到项目详情页
      setTimeout(() => {
        navigate(`/project/${pid}`);
      }, 1000);
    }
  };


  // 获取步骤状态图标和样式
  const getStepStatus = (step: GenerationStep) => {
    if (step === 'completed') {
      return {
        icon: <CheckCircleOutlined />,
        color: token.colorSuccess,
        text: t('statusCompleted'),
        background: `linear-gradient(135deg, ${alphaColor(token.colorSuccess, 0.12)} 0%, ${token.colorBgContainer} 100%)`,
        borderColor: alphaColor(token.colorSuccess, 0.28),
      };
    }

    if (step === 'processing') {
      return {
        icon: <LoadingOutlined spin />,
        color: token.colorPrimary,
        text: t('statusProcessing'),
        background: `linear-gradient(135deg, ${alphaColor(token.colorPrimary, 0.14)} 0%, ${token.colorBgContainer} 100%)`,
        borderColor: alphaColor(token.colorPrimary, 0.32),
      };
    }

    if (step === 'error') {
      return {
        icon: '✕',
        color: token.colorError,
        text: t('statusFailed'),
        background: `linear-gradient(135deg, ${alphaColor(token.colorError, 0.12)} 0%, ${token.colorBgContainer} 100%)`,
        borderColor: alphaColor(token.colorError, 0.32),
      };
    }

    return {
      icon: '○',
      color: token.colorTextQuaternary,
      text: t('statusPending'),
      background: token.colorFillQuaternary,
      borderColor: token.colorBorderSecondary,
    };
  };

  const hasError = generationSteps.worldBuilding === 'error' ||
    generationSteps.careers === 'error' ||
    generationSteps.characters === 'error' ||
    generationSteps.outline === 'error';

  const progressAccentColor = hasError
    ? token.colorError
    : progress === 100
      ? token.colorSuccess
      : token.colorPrimary;

  const stepItems = [
    { key: 'worldBuilding', label: t('stepWorld'), step: generationSteps.worldBuilding },
    { key: 'careers', label: t('stepCareers'), step: generationSteps.careers },
    { key: 'characters', label: t('stepCharacters'), step: generationSteps.characters },
    { key: 'outline', label: t('stepOutline'), step: generationSteps.outline },
  ];

  const availableViewportHeight = isMobile
    ? 'calc(100dvh - 96px)'
    : 'calc(100dvh - 128px)';

  // 渲染生成进度页面
  const renderGenerating = () => (
    <div
      style={{
        padding: isMobile ? '4px 0 8px' : '8px 0 12px',
        maxWidth: 920,
        margin: '0 auto',
        overflow: 'hidden',
        minHeight: availableViewportHeight,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: hasError ? 'flex-start' : 'center',
      }}
    >
      <div
        style={{
          marginBottom: 14,
          padding: isMobile ? '18px 16px' : '24px 24px 20px',
          borderRadius: 18,
          border: `1px solid ${alphaColor(hasError ? token.colorError : token.colorPrimary, 0.18)}`,
          background: `linear-gradient(135deg, ${alphaColor(token.colorPrimary, 0.12)} 0%, ${token.colorBgContainer} 48%, ${alphaColor(hasError ? token.colorError : token.colorSuccess, hasError ? 0.08 : 0.04)} 100%)`,
          boxShadow: `0 12px 28px ${alphaColor(token.colorText, 0.06)}`,
          textAlign: 'center',
        }}
      >
        <Title
          level={isMobile ? 4 : 3}
          style={{
            marginBottom: 8,
            color: token.colorTextHeading,
            wordBreak: 'break-word',
            whiteSpace: 'normal',
            overflowWrap: 'break-word',
          }}
        >
          {t('generatingTitle', { title: config.title })}
        </Title>

        <Paragraph
          style={{
            maxWidth: 620,
            margin: '0 auto',
            color: token.colorTextSecondary,
            fontSize: isMobile ? 13 : 14,
            lineHeight: 1.7,
            wordBreak: 'break-word',
            whiteSpace: 'normal',
            overflowWrap: 'break-word',
          }}
        >
          {hasError
            ? t('errorInterrupted')
            : t('normalDescription')}
        </Paragraph>
      </div>

      <Card
        style={{
          marginBottom: 12,
          borderRadius: 18,
          border: `1px solid ${alphaColor(token.colorText, 0.08)}`,
          background: `linear-gradient(180deg, ${alphaColor(token.colorBgContainer, 0.97)} 0%, ${alphaColor(token.colorPrimary, 0.03)} 100%)`,
          boxShadow: `0 10px 24px ${alphaColor(token.colorText, 0.06)}`,
        }}
        styles={{
          body: {
            padding: isMobile ? 14 : 20,
          }
        }}
      >
        <div
          style={{
            padding: isMobile ? '14px 14px 16px' : '16px 18px 18px',
            marginBottom: 16,
            borderRadius: 14,
            background: token.colorFillQuaternary,
            border: `1px solid ${alphaColor(progressAccentColor, 0.18)}`,
          }}
        >
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: isMobile ? 'flex-start' : 'center',
              flexDirection: isMobile ? 'column' : 'row',
              gap: 10,
              marginBottom: 10,
            }}
          >
            <div style={{ flex: 1, textAlign: 'left' }}>
              <Text
                style={{
                  display: 'block',
                  marginBottom: 6,
                  color: token.colorTextTertiary,
                  fontSize: 12,
                  letterSpacing: 0.4,
                }}
              >
                {t('currentProgress')}
              </Text>
              <Paragraph
                style={{
                  margin: 0,
                  color: hasError ? token.colorError : token.colorText,
                  fontSize: isMobile ? 13 : 15,
                  lineHeight: 1.7,
                  wordBreak: 'break-word',
                  whiteSpace: 'normal',
                  overflowWrap: 'break-word',
                }}
              >
                {progressMessage || t('preparing')}
              </Paragraph>
            </div>

            <div
              style={{
                minWidth: isMobile ? 'auto' : 96,
                textAlign: isMobile ? 'left' : 'right',
              }}
            >
              <Text
                style={{
                  fontSize: isMobile ? 24 : 32,
                  lineHeight: 1,
                  fontWeight: 700,
                  color: progressAccentColor,
                }}
              >
                {progress}%
              </Text>
            </div>
          </div>

          <Progress
            percent={progress}
            showInfo={false}
            status={hasError ? 'exception' : (progress === 100 ? 'success' : 'active')}
            strokeColor={progress === 100
              ? {
                '0%': token.colorSuccess,
                '100%': token.colorSuccessActive,
              }
              : {
                '0%': token.colorPrimary,
                '100%': token.colorPrimaryActive,
              }}
            trailColor={token.colorFillTertiary}
            strokeLinecap="round"
            style={{ marginBottom: 0 }}
          />
        </div>

        {errorDetails && (
          <div
            style={{
              marginBottom: 16,
              padding: isMobile ? '12px 14px' : '14px 16px',
              borderRadius: 14,
              background: `linear-gradient(135deg, ${alphaColor(token.colorError, 0.12)} 0%, ${token.colorBgContainer} 100%)`,
              border: `1px solid ${alphaColor(token.colorError, 0.24)}`,
              textAlign: 'left',
              overflow: 'hidden',
            }}
          >
            <Text strong style={{ color: token.colorError, display: 'block', marginBottom: 8 }}>
              {t('errorDetails')}
            </Text>
            <Text
              style={{
                color: token.colorTextSecondary,
                fontSize: 14,
                lineHeight: 1.7,
                wordBreak: 'break-word',
                whiteSpace: 'normal',
                overflowWrap: 'break-word',
                display: 'block',
              }}
            >
              {errorDetails}
            </Text>
          </div>
        )}

        <div
          style={{
            display: 'grid',
            gap: 10,
          }}
        >
          {stepItems.map(({ key, label, step }) => {
            const status = getStepStatus(step);
            return (
              <div
                key={key}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: isMobile ? '10px 12px' : '12px 14px',
                  background: status.background,
                  borderRadius: 14,
                  border: `1px solid ${status.borderColor}`,
                  gap: 12,
                  maxWidth: '100%',
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 12,
                    flex: 1,
                    minWidth: 0,
                  }}
                >
                  <span
                    style={{
                      width: 30,
                      height: 30,
                      borderRadius: '50%',
                      display: 'inline-flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: status.color,
                      background: alphaColor(status.color, 0.12),
                      fontSize: 18,
                      flexShrink: 0,
                    }}
                  >
                    {status.icon}
                  </span>

                  <div
                    style={{
                      minWidth: 0,
                      flex: 1,
                      textAlign: 'left',
                    }}
                  >
                    <Text
                      style={{
                        display: 'block',
                        fontSize: isMobile ? 13 : 14,
                        fontWeight: step === 'processing' ? 600 : 500,
                        color: token.colorText,
                        wordBreak: 'break-word',
                        whiteSpace: 'normal',
                        overflowWrap: 'break-word',
                      }}
                    >
                      {label}
                    </Text>
                    <Text
                      style={{
                        fontSize: 12,
                        color: step === 'pending' ? token.colorTextTertiary : status.color,
                      }}
                    >
                      {status.text}
                    </Text>
                  </div>
                </div>

                <Text
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    color: status.color,
                    padding: '4px 10px',
                    borderRadius: 999,
                    background: alphaColor(status.color, 0.1),
                    whiteSpace: 'nowrap',
                    flexShrink: 0,
                  }}
                >
                  {status.text}
                </Text>
              </div>
            );
          })}
        </div>
      </Card>

      <Paragraph
        type="secondary"
        style={{
          marginBottom: hasError ? 14 : 0,
          color: token.colorTextSecondary,
          opacity: 0.92,
          textAlign: 'center',
          wordBreak: 'break-word',
          whiteSpace: 'normal',
          overflowWrap: 'break-word',
          fontSize: isMobile ? 13 : 14,
        }}
      >
        {hasError ? t('footerError') : t('footerNormal')}
      </Paragraph>

      {hasError && (
        <Space
          direction={isMobile ? 'vertical' : 'horizontal'}
          style={{ width: '100%', justifyContent: 'center' }}
        >
          <Button
            type="primary"
            size="large"
            onClick={handleSmartRetry}
            loading={loading}
            disabled={loading}
            style={{
              minWidth: isMobile ? '100%' : 160,
              height: 44,
              borderRadius: 12,
              boxShadow: `0 10px 24px ${alphaColor(token.colorPrimary, 0.22)}`,
            }}
          >
            {t('smartRetry')}
          </Button>
          {onBack && (
            <Button
              size="large"
              danger
              onClick={handleRestartGeneration}
              disabled={loading}
              style={{
                minWidth: isMobile ? '100%' : 160,
                height: 44,
                borderRadius: 12,
              }}
            >
              {t('restartGeneration')}
            </Button>
          )}
        </Space>
      )}
    </div>
  );

  return renderGenerating();
};
