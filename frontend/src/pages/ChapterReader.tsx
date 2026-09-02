import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Card, Spin, Alert, Button, Space, Switch, Drawer, message, Progress, theme } from 'antd';
import {
  ArrowLeftOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
  MenuOutlined,
  ReloadOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons';
import api from '../services/api';
import AnnotatedText, { type MemoryAnnotation } from '../components/AnnotatedText';
import MemorySidebar from '../components/MemorySidebar';
import { useTranslation } from 'react-i18next';

interface ChapterData {
  id: string;
  chapter_number: number;
  title: string;
  content: string;
  word_count: number;
}

interface AnnotationsData {
  chapter_id: string;
  chapter_number: number;
  title: string;
  word_count: number;
  annotations: MemoryAnnotation[];
  has_analysis: boolean;
  summary: {
    total_annotations: number;
    hooks: number;
    foreshadows: number;
    plot_points: number;
    character_events: number;
  };
}

interface NavigationData {
  current: {
    id: string;
    chapter_number: number;
    title: string;
  };
  previous: {
    id: string;
    chapter_number: number;
    title: string;
  } | null;
  next: {
    id: string;
    chapter_number: number;
    title: string;
  } | null;
}

interface AnalysisTaskStatus {
  status: 'none' | 'pending' | 'running' | 'completed' | 'failed';
  progress: number;
  error_message?: string | null;
}

const ANALYSIS_POLL_TIMEOUT_MS = 11 * 60 * 1000;

/**
 * 章节阅读器页面
 * 展示带有记忆标注的章节内容
 */
const ChapterReader: React.FC = () => {
  const { t } = useTranslation('chapterReader');
  const { chapterId } = useParams<{ chapterId: string }>();
  const navigate = useNavigate();

  const { token } = theme.useToken();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [chapter, setChapter] = useState<ChapterData | null>(null);
  const [annotationsData, setAnnotationsData] = useState<AnnotationsData | null>(null);
  const [showAnnotations, setShowAnnotations] = useState(true);
  const [activeAnnotationId, setActiveAnnotationId] = useState<string | undefined>();
  const [sidebarVisible, setSidebarVisible] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisProgress, setAnalysisProgress] = useState(0);
  const [navigation, setNavigation] = useState<NavigationData | null>(null);
  const analysisPollTimerRef = useRef<number | null>(null);
  const analysisRunIdRef = useRef(0);

  const loadChapterData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      // 并行加载章节内容、标注数据和导航信息
      // 注意：api拦截器已经解析了response.data，所以直接返回数据对象
      const [chapterData, annotationsData, navigationData] = await Promise.all([
        api.get<unknown, ChapterData>(`/chapters/${chapterId}`).catch(err => {
          console.error('加载章节失败:', err);
          throw err;
        }),
        api.get<unknown, AnnotationsData>(`/chapters/${chapterId}/annotations`).catch(err => {
          console.warn('加载标注失败:', err);
          return null;
        }), // 如果没有分析数据也不报错
        api.get<unknown, NavigationData>(`/chapters/${chapterId}/navigation`).catch(err => {
          console.warn('加载导航信息失败:', err);
          return null;
        }),
      ]);

      console.log('章节数据:', chapterData);
      console.log('标注数据:', annotationsData);
      console.log('导航数据:', navigationData);

      // 验证数据
      if (!chapterData || !chapterData.content) {
        throw new Error(t('error.invalidChapterData'));
      }

      setChapter(chapterData);
      setNavigation(navigationData);
      
      // 验证标注数据
      if (annotationsData) {
        const validAnnotations = annotationsData.annotations.filter(
          (a: MemoryAnnotation) => a.position >= 0 && a.position < chapterData.content.length
        );
        const invalidCount = annotationsData.annotations.length - validAnnotations.length;
        
        if (invalidCount > 0) {
          console.warn(`${invalidCount}个标注位置无效，将仅显示${validAnnotations.length}个有效标注`);
        }
        
        setAnnotationsData(annotationsData);
      } else {
        setAnnotationsData(null);
      }
    } catch (err: unknown) {
      console.error('加载章节数据失败:', err);
      const error = err as { response?: { data?: { detail?: string } }; message?: string };
      setError(error.response?.data?.detail || error.message || t('error.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [chapterId]);

  useEffect(() => {
    if (chapterId) {
      loadChapterData();
    }
  }, [chapterId, loadChapterData]);

  useEffect(() => {
    return () => {
      analysisRunIdRef.current += 1;
      if (analysisPollTimerRef.current !== null) {
        window.clearTimeout(analysisPollTimerRef.current);
        analysisPollTimerRef.current = null;
      }
    };
  }, [chapterId]);

  const handleAnnotationClick = (annotation: MemoryAnnotation) => {
    setActiveAnnotationId(annotation.id);
    // 移动端显示侧边栏
    if (window.innerWidth < 768) {
      setSidebarVisible(true);
    }
  };

  const handleBackClick = () => {
    navigate(-1);
  };

  const handlePreviousChapter = () => {
    if (navigation?.previous) {
      navigate(`/chapters/${navigation.previous.id}/reader`);
    }
  };

  const handleNextChapter = () => {
    if (navigation?.next) {
      navigate(`/chapters/${navigation.next.id}/reader`);
    }
  };

  const handleReanalyze = async () => {
    if (!chapterId) return;

    const requestedChapterId = chapterId;
    const runId = analysisRunIdRef.current + 1;
    analysisRunIdRef.current = runId;
    const deadline = Date.now() + ANALYSIS_POLL_TIMEOUT_MS;

    if (analysisPollTimerRef.current !== null) {
      window.clearTimeout(analysisPollTimerRef.current);
      analysisPollTimerRef.current = null;
    }

    try {
      setAnalyzing(true);
      setAnalysisProgress(0);
      message.loading({ content: t('analyze.starting'), key: 'analyze', duration: 0 });

      // 触发分析
      await api.post(`/chapters/${requestedChapterId}/analyze`);

      const pollStatus = async (): Promise<void> => {
        if (analysisRunIdRef.current !== runId) return;

        if (Date.now() >= deadline) {
          setAnalyzing(false);
          message.warning({ content: t('analyze.timeout'), key: 'analyze' });
          return;
        }

        try {
          const statusRes = await api.get<unknown, AnalysisTaskStatus>(
            `/chapters/${requestedChapterId}/analysis/status`
          );
          if (analysisRunIdRef.current !== runId) return;

          const { status, progress, error_message } = statusRes;

          setAnalysisProgress(progress || 0);

          if (status === 'completed') {
            setAnalyzing(false);
            message.success({ content: t('analyze.done'), key: 'analyze' });
            const annotationsRes = await api.get<unknown, AnnotationsData>(
              `/chapters/${requestedChapterId}/annotations`
            );
            if (analysisRunIdRef.current === runId) {
              setAnnotationsData(annotationsRes);
            }
            return;
          } else if (status === 'failed') {
            setAnalyzing(false);
            message.error({
              content: t('analyze.failed', { error: error_message || t('analyze.unknownError') }),
              key: 'analyze'
            });
            return;
          }
        } catch (err) {
          console.error('轮询分析状态失败:', err);
        }

        if (analysisRunIdRef.current === runId) {
          analysisPollTimerRef.current = window.setTimeout(() => {
            void pollStatus();
          }, 2000);
        }
      };

      void pollStatus();
    } catch (err: unknown) {
      if (analysisRunIdRef.current === runId) {
        setAnalyzing(false);
      }
      const error = err as { response?: { data?: { detail?: string } } };
      message.error({
        content: error.response?.data?.detail || t('analyze.triggerFailed'),
        key: 'analyze'
      });
    }
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: '100px 0' }}>
        <Spin size="large" tip={t('status.loadingChapter')} />
      </div>
    );
  }

  if (error || !chapter) {
    return (
      <div style={{ padding: 24 }}>
        <Alert
          message={t('error.loadFailed')}
          description={error || t('error.chapterNotFound')}
          type="error"
          showIcon
        />
        <Button onClick={handleBackClick} style={{ marginTop: 16 }}>
          {t('actions.back')}
        </Button>
      </div>
    );
  }

  const hasAnnotations = annotationsData && annotationsData.annotations.length > 0;

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* 顶部工具栏 */}
      <Card
        size="small"
        style={{
          borderRadius: 0,
          borderLeft: 0,
          borderRight: 0,
          borderTop: 0,
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Space>
            <Button icon={<ArrowLeftOutlined />} onClick={handleBackClick}>
              {t('actions.back')}
            </Button>
            <Button
              icon={<LeftOutlined />}
              onClick={handlePreviousChapter}
              disabled={!navigation?.previous}
              title={navigation?.previous ? t('nav.prevTitle', { title: navigation.previous.title }) : t('nav.firstChapter')}
            >
              {t('nav.previous')}
            </Button>
            <span style={{ fontSize: 16, fontWeight: 600 }}>
              {t('nav.chapterLabel', { n: chapter.chapter_number, title: chapter.title })}
            </span>
            <Button
              icon={<RightOutlined />}
              onClick={handleNextChapter}
              disabled={!navigation?.next}
              title={navigation?.next ? t('nav.nextTitle', { title: navigation.next.title }) : t('nav.lastChapter')}
            >
              {t('nav.next')}
            </Button>
          </Space>

          <Space>
            <Button
              icon={<ReloadOutlined />}
              onClick={handleReanalyze}
              loading={analyzing}
              disabled={analyzing}
            >
              {analyzing ? t('analyze.analyzing') : t('analyze.reanalyze')}
            </Button>
            {hasAnnotations && (
              <>
                <Switch
                  checked={showAnnotations}
                  onChange={setShowAnnotations}
                  checkedChildren={<EyeOutlined />}
                  unCheckedChildren={<EyeInvisibleOutlined />}
                />
                <span style={{ fontSize: 13, color: token.colorTextSecondary }}>{t('actions.showAnnotations')}</span>
                <Button
                  icon={<MenuOutlined />}
                  onClick={() => setSidebarVisible(true)}
                  style={{ display: window.innerWidth < 768 ? 'inline-block' : 'none' }}
                >
                  {t('actions.analyze')}
                </Button>
              </>
            )}
          </Space>
        </div>

        {analyzing && (
          <div style={{ marginTop: 12 }}>
            <Progress percent={analysisProgress} size="small" status="active" />
            <span style={{ fontSize: 12, color: token.colorTextSecondary, marginLeft: 8 }}>
              {t('analyze.inProgress')}
            </span>
          </div>
        )}

        {!analyzing && hasAnnotations && annotationsData && (
          <div style={{ marginTop: 12, fontSize: 12, color: token.colorTextTertiary }}>
            {t('summary.total', { n: annotationsData.summary.total_annotations })}
            {annotationsData.summary.hooks > 0 && t('summary.hooks', { n: annotationsData.summary.hooks })}
            {annotationsData.summary.foreshadows > 0 &&
              t('summary.foreshadows', { n: annotationsData.summary.foreshadows })}
            {annotationsData.summary.plot_points > 0 &&
              t('summary.plotPoints', { n: annotationsData.summary.plot_points })}
            {annotationsData.summary.character_events > 0 &&
              t('summary.characterEvents', { n: annotationsData.summary.character_events })}
          </div>
        )}
      </Card>

      {/* 主内容区域 */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
        {/* 左侧：章节内容 */}
        <div
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: '32px 48px',
            maxWidth: hasAnnotations ? 'calc(100% - 400px)' : '100%',
          }}
        >
          <Card>
            <div style={{ maxWidth: 800, margin: '0 auto' }}>
              {!hasAnnotations && (
                <Alert
                  message={t('empty.noAnalysisTitle')}
                  description={t('empty.noAnalysisDesc')}
                  type="info"
                  showIcon
                  style={{ marginBottom: 24 }}
                />
              )}

              {showAnnotations && hasAnnotations && annotationsData ? (
                <AnnotatedText
                  content={chapter.content}
                  annotations={annotationsData.annotations}
                  onAnnotationClick={handleAnnotationClick}
                  activeAnnotationId={activeAnnotationId}
                />
              ) : (
                <div
                  style={{
                    lineHeight: 2,
                    fontSize: 16,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                  }}
                >
                  {chapter.content}
                </div>
              )}

              {/* 底部翻页按钮 */}
              <div style={{ marginTop: 48, paddingTop: 24, borderTop: `1px solid ${token.colorBorderSecondary}` }}>
                <Space style={{ width: '100%', justifyContent: 'space-between' }}>
                  <Button
                    size="large"
                    icon={<LeftOutlined />}
                    onClick={handlePreviousChapter}
                    disabled={!navigation?.previous}
                  >
                    {navigation?.previous
                      ? t('nav.prevFull', { n: navigation.previous.chapter_number, title: navigation.previous.title })
                      : t('nav.firstChapter')}
                  </Button>
                  <Button
                    size="large"
                    type="primary"
                    icon={<RightOutlined />}
                    onClick={handleNextChapter}
                    disabled={!navigation?.next}
                    iconPosition="end"
                  >
                    {navigation?.next
                      ? t('nav.nextFull', { n: navigation.next.chapter_number, title: navigation.next.title })
                      : t('nav.lastChapter')}
                  </Button>
                </Space>
              </div>
            </div>
          </Card>
        </div>

        {/* 右侧：记忆侧边栏（桌面端） */}
        {hasAnnotations && annotationsData && window.innerWidth >= 768 && (
          <div
            style={{
              width: 400,
              borderLeft: `1px solid ${token.colorBorderSecondary}`,
              overflowY: 'auto',
              background: token.colorBgLayout,
            }}
          >
            <MemorySidebar
              annotations={annotationsData.annotations}
              activeAnnotationId={activeAnnotationId}
              onAnnotationClick={handleAnnotationClick}
            />
          </div>
        )}
      </div>

      {/* 移动端抽屉 */}
      {hasAnnotations && annotationsData && (
        <Drawer
          title={t('drawer.title')}
          placement="right"
          onClose={() => setSidebarVisible(false)}
          open={sidebarVisible}
          width="80%"
        >
          <MemorySidebar
            annotations={annotationsData.annotations}
            activeAnnotationId={activeAnnotationId}
            onAnnotationClick={(annotation) => {
              handleAnnotationClick(annotation);
              setSidebarVisible(false);
            }}
          />
        </Drawer>
      )}
    </div>
  );
};

export default ChapterReader;
