import React, { useState, useEffect } from 'react';
import { Card, List, Button, Space, Empty, Tag, Spin, Alert, Switch, Drawer, message, theme } from 'antd';
import {
  EyeOutlined,
  EyeInvisibleOutlined,
  MenuOutlined,
  LeftOutlined,
  RightOutlined,
  UnorderedListOutlined,
  FundOutlined,
} from '@ant-design/icons';
import { useParams } from 'react-router-dom';
import api from '../services/api';
import AnnotatedText, { type MemoryAnnotation } from '../components/AnnotatedText';
import MemorySidebar from '../components/MemorySidebar';
import { eventBus, EventNames } from '../store/eventBus';
import { useTranslation } from 'react-i18next';

interface ChapterItem {
  id: string;
  chapter_number: number;
  title: string;
  content: string;
  word_count: number;
  status: string;
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

/**
 * 项目内的章节剧情分析页面
 * 显示章节列表和带标注的章节内容
 */
const ChapterAnalysis: React.FC = () => {
  const { t } = useTranslation('chapterAnalysis');
  const { projectId } = useParams<{ projectId: string }>();
  
  const [chapters, setChapters] = useState<ChapterItem[]>([]);
  const [selectedChapter, setSelectedChapter] = useState<ChapterItem | null>(null);
  const [annotationsData, setAnnotationsData] = useState<AnnotationsData | null>(null);
  const [navigation, setNavigation] = useState<NavigationData | null>(null);
  const [loading, setLoading] = useState(true);
  const [contentLoading, setContentLoading] = useState(false);
  const [showAnnotations, setShowAnnotations] = useState(true);
  const [activeAnnotationId, setActiveAnnotationId] = useState<string | undefined>();
  const [sidebarVisible, setSidebarVisible] = useState(false);
  const [chapterListVisible, setChapterListVisible] = useState(false);
  const [scrollToContentAnnotation, setScrollToContentAnnotation] = useState<string | undefined>();
  const [scrollToSidebarAnnotation, setScrollToSidebarAnnotation] = useState<string | undefined>();
  const [isMobile, setIsMobile] = useState(window.innerWidth < 768);
  const { token } = theme.useToken();

  // 监听窗口大小变化
  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth < 768);
    };
    
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // 加载章节列表
  useEffect(() => {
    const loadChapters = async () => {
      if (!projectId) return;
      
      try {
        setLoading(true);
        const response = await api.get(`/chapters/project/${projectId}`);
        // API 拦截器已经解析了 response.data，所以直接使用
        const data = response.data || response;
        const chapterList = data.items || [];
        setChapters(chapterList);
        
        // 自动选择第一个有内容的章节
        const firstChapterWithContent = chapterList.find((ch: ChapterItem) => ch.content && ch.content.trim() !== '');
        if (firstChapterWithContent) {
          loadChapterContent(firstChapterWithContent.id);
        }
      } catch (error) {
        console.error('加载章节列表失败:', error);
        message.error(t('toast.loadListFailed'));
      } finally {
        setLoading(false);
      }
    };

    loadChapters();
  }, [projectId]);

  // 加载章节内容和标注
  const loadChapterContent = async (chapterId: string) => {
    try {
      setContentLoading(true);
      
      const [chapterResponse, annotationsResponse, navigationResponse] = await Promise.all([
        api.get(`/chapters/${chapterId}`),
        api.get(`/chapters/${chapterId}/annotations`).catch(() => null),
        api.get(`/chapters/${chapterId}/navigation`).catch(() => null),
      ]);

      // 提取 data 属性
      setSelectedChapter(chapterResponse.data || chapterResponse);
      setAnnotationsData(annotationsResponse ? (annotationsResponse.data || annotationsResponse) : null);
      setNavigation(navigationResponse ? (navigationResponse.data || navigationResponse) : null);
    } catch (error) {
      console.error('加载章节内容失败:', error);
      message.error(t('toast.loadContentFailed'));
    } finally {
      setContentLoading(false);
    }
  };

  useEffect(() => {
    const handleTaskSettled = (payload?: unknown) => {
      if (!payload || typeof payload !== 'object') return;
      const data = payload as { projectId?: string; resources?: string[] };
      if (data.projectId && data.projectId !== projectId) return;
      if (data.resources?.includes('analysis') && selectedChapter?.id) {
        void loadChapterContent(selectedChapter.id);
      }
    };
    eventBus.on(EventNames.BACKGROUND_TASK_SETTLED, handleTaskSettled);
    return () => eventBus.off(EventNames.BACKGROUND_TASK_SETTLED, handleTaskSettled);
  }, [projectId, selectedChapter?.id]);

  const handleChapterSelect = (chapterId: string) => {
    loadChapterContent(chapterId);
    if (isMobile) {
      setChapterListVisible(false);
    }
  };

  const handlePreviousChapter = () => {
    if (navigation?.previous) {
      loadChapterContent(navigation.previous.id);
    }
  };

  const handleNextChapter = () => {
    if (navigation?.next) {
      loadChapterContent(navigation.next.id);
    }
  };

  const handleAnnotationClick = (annotation: MemoryAnnotation, source: 'content' | 'sidebar' = 'content') => {
    setActiveAnnotationId(annotation.id);
    
    if (source === 'content') {
      // 从内容区点击，滚动到侧边栏
      setScrollToSidebarAnnotation(annotation.id);
      // 清除滚动状态
      setTimeout(() => setScrollToSidebarAnnotation(undefined), 100);
      
      if (isMobile) {
        setSidebarVisible(true);
      }
    } else {
      // 从侧边栏点击，滚动到内容区
      setScrollToContentAnnotation(annotation.id);
      // 清除滚动状态
      setTimeout(() => setScrollToContentAnnotation(undefined), 100);
    }
  };

  const hasAnnotations = annotationsData && annotationsData.annotations.length > 0;

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: '100px 0' }}>
        <Spin size="large" tip={t('status.loadingChapters')} />
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* 页面标题 - 仅桌面端显示 */}
      {!isMobile && (
        <div style={{
          padding: '16px 0',
          marginBottom: 16,
          borderBottom: `1px solid ${token.colorBorderSecondary}`
        }}>
          <h2 style={{ margin: 0, fontSize: 24 }}>
            <FundOutlined style={{ marginRight: 8 }} />
            {t('page.title')}
          </h2>
        </div>
      )}
      
      <div style={{
        flex: 1,
        display: 'flex',
        gap: isMobile ? 0 : 16,
        flexDirection: isMobile ? 'column' : 'row',
        overflow: 'hidden'
      }}>
        {/* 左侧章节列表 - 桌面端 */}
        {!isMobile && (
        <Card
          title={t('list.title')}
          style={{ width: 280, height: '100%', overflow: 'hidden' }}
          bodyStyle={{ padding: 0, height: 'calc(100% - 57px)', overflow: 'auto' }}
        >
          {chapters.length === 0 ? (
            <Empty description={t('list.empty')} style={{ marginTop: 60 }} />
          ) : (
            <List
              dataSource={chapters}
              renderItem={(chapter) => (
                <List.Item
                  key={chapter.id}
                  onClick={() => handleChapterSelect(chapter.id)}
                  style={{
                    cursor: 'pointer',
                    padding: '12px 16px',
                    background: selectedChapter?.id === chapter.id ? token.colorPrimaryBg : 'transparent',
                    borderLeft: selectedChapter?.id === chapter.id ? `3px solid ${token.colorPrimary}` : '3px solid transparent',
                  }}
                >
                  <List.Item.Meta
                    title={
                      <span style={{ fontSize: 14, fontWeight: selectedChapter?.id === chapter.id ? 600 : 400 }}>
                        {t('list.chapterLabel', { n: chapter.chapter_number, title: chapter.title })}
                      </span>
                    }
                    description={
                      <Space size={4}>
                        <Tag color={chapter.content && chapter.content.trim() !== '' ? 'success' : 'default'}>
                          {t('list.wordCount', { n: chapter.word_count || 0 })}
                        </Tag>
                      </Space>
                    }
                  />
                </List.Item>
              )}
            />
          )}
        </Card>
        )}

        {/* 移动端章节列表抽屉 */}
      {isMobile && (
        <Drawer
          title={t('list.title')}
          placement="left"
          onClose={() => setChapterListVisible(false)}
          open={chapterListVisible}
          width="85%"
          styles={{ body: { padding: 0 } }}
        >
          {chapters.length === 0 ? (
            <Empty description={t('list.empty')} style={{ marginTop: 60 }} />
          ) : (
            <List
              dataSource={chapters}
              renderItem={(chapter) => (
                <List.Item
                  key={chapter.id}
                  onClick={() => handleChapterSelect(chapter.id)}
                  style={{
                    cursor: 'pointer',
                    padding: '12px 16px',
                    background: selectedChapter?.id === chapter.id ? token.colorPrimaryBg : 'transparent',
                    borderLeft: selectedChapter?.id === chapter.id ? `3px solid ${token.colorPrimary}` : '3px solid transparent',
                  }}
                >
                  <List.Item.Meta
                    title={
                      <span style={{ fontSize: 14, fontWeight: selectedChapter?.id === chapter.id ? 600 : 400 }}>
                        {t('list.chapterLabel', { n: chapter.chapter_number, title: chapter.title })}
                      </span>
                    }
                    description={
                      <Space size={4}>
                        <Tag color={chapter.content && chapter.content.trim() !== '' ? 'success' : 'default'}>
                          {t('list.wordCount', { n: chapter.word_count || 0 })}
                        </Tag>
                      </Space>
                    }
                  />
                </List.Item>
              )}
            />
          )}
        </Drawer>
        )}

        {/* 右侧内容区域 */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
        {!selectedChapter ? (
          <Card style={{ height: '100%' }}>
            <Empty description={t('empty.selectChapter')} style={{ marginTop: 100 }} />
          </Card>
        ) : (
          <>
            {/* 工具栏 */}
            <Card size="small" style={{ marginBottom: isMobile ? 8 : 16 }}>
              {isMobile ? (
                // 移动端布局：两行显示
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {/* 第一行：标题和翻页按钮 */}
                  <div style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: 8
                  }}>
                    <Button
                      icon={<LeftOutlined />}
                      onClick={handlePreviousChapter}
                      disabled={!navigation?.previous}
                      title={navigation?.previous ? t('nav.prevTitle', { title: navigation.previous.title }) : t('nav.firstChapter')}
                      size="small"
                    />
                    <span style={{
                      fontSize: 14,
                      fontWeight: 600,
                      flex: 1,
                      textAlign: 'center',
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      padding: '0 8px'
                    }}>
                      {t('nav.chapterLabel', { n: selectedChapter.chapter_number, title: selectedChapter.title })}
                    </span>
                    <Button
                      icon={<RightOutlined />}
                      onClick={handleNextChapter}
                      disabled={!navigation?.next}
                      title={navigation?.next ? t('nav.nextTitle', { title: navigation.next.title }) : t('nav.lastChapter')}
                      size="small"
                    />
                  </div>

                  {/* 第二行：章节、开关、分析按钮 */}
                  <div style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: 8
                  }}>
                    <Button
                      icon={<UnorderedListOutlined />}
                      onClick={() => setChapterListVisible(true)}
                      size="small"
                    >
                      {t('nav.chapters')}
                    </Button>

                    {hasAnnotations && (
                      <>
                        <Switch
                          checked={showAnnotations}
                          onChange={setShowAnnotations}
                          checkedChildren={<EyeOutlined />}
                          unCheckedChildren={<EyeInvisibleOutlined />}
                          size="small"
                          style={{
                            flexShrink: 0,
                            height: 16,
                            minHeight: 16,
                            lineHeight: '16px'
                          }}
                        />
                        <Button
                          icon={<MenuOutlined />}
                          onClick={() => setSidebarVisible(true)}
                          size="small"
                        >
                          {t('actions.analyze')}
                        </Button>
                      </>
                    )}
                  </div>
                </div>
              ) : (
                // 桌面端布局：保持原样
                <div style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center'
                }}>
                  <Space>
                    <Button
                      icon={<LeftOutlined />}
                      onClick={handlePreviousChapter}
                      disabled={!navigation?.previous}
                      title={navigation?.previous ? t('nav.prevTitle', { title: navigation.previous.title }) : t('nav.firstChapter')}
                    >
                      {t('nav.previous')}
                    </Button>
                    <span style={{ fontSize: 16, fontWeight: 600 }}>
                      {t('nav.chapterLabel', { n: selectedChapter.chapter_number, title: selectedChapter.title })}
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
                    {hasAnnotations && (
                      <>
                        <Switch
                          checked={showAnnotations}
                          onChange={setShowAnnotations}
                          checkedChildren={<EyeOutlined />}
                          unCheckedChildren={<EyeInvisibleOutlined />}
                        />
                        <span style={{ fontSize: 13, color: token.colorTextSecondary }}>{t('actions.showAnnotations')}</span>
                      </>
                    )}
                  </Space>
                </div>
              )}

              {hasAnnotations && annotationsData && (
                <div style={{
                  marginTop: 12,
                  fontSize: isMobile ? 11 : 12,
                  color: token.colorTextTertiary,
                  lineHeight: 1.5
                }}>
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

            {/* 内容区域 */}
            <div style={{
              flex: 1,
              display: 'flex',
              gap: isMobile ? 0 : 16,
              overflow: 'hidden'
            }}>
              {/* 章节内容 */}
              <Card
                style={{ flex: 1, overflow: 'auto' }}
                bodyStyle={{ padding: isMobile ? '12px' : '24px' }}
                loading={contentLoading}
              >
                {!contentLoading && (
                  <>
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
                        content={selectedChapter.content}
                        annotations={annotationsData.annotations}
                        onAnnotationClick={(annotation) => handleAnnotationClick(annotation, 'content')}
                        activeAnnotationId={activeAnnotationId}
                        scrollToAnnotation={scrollToContentAnnotation}
                        style={{
                          lineHeight: isMobile ? 1.8 : 2,
                          fontSize: isMobile ? 14 : 16,
                        }}
                      />
                    ) : (
                      <div
                        style={{
                          lineHeight: isMobile ? 1.8 : 2,
                          fontSize: isMobile ? 14 : 16,
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                        }}
                      >
                        {selectedChapter.content}
                      </div>
                    )}
                  </>
                )}
              </Card>

              {/* 右侧记忆侧边栏（桌面端） */}
              {hasAnnotations && annotationsData && !isMobile && (
                <Card
                  style={{ width: 400, overflow: 'auto' }}
                  bodyStyle={{ padding: 0 }}
                >
                  <MemorySidebar
                    annotations={annotationsData.annotations}
                    activeAnnotationId={activeAnnotationId}
                    onAnnotationClick={(annotation) => handleAnnotationClick(annotation, 'sidebar')}
                    scrollToAnnotation={scrollToSidebarAnnotation}
                  />
                </Card>
              )}
            </div>

            {/* 移动端抽屉 */}
            {hasAnnotations && annotationsData && (
              <Drawer
                title={t('drawer.title')}
                placement="right"
                onClose={() => setSidebarVisible(false)}
                open={sidebarVisible}
                width={isMobile ? '90%' : '80%'}
              >
                <MemorySidebar
                  annotations={annotationsData.annotations}
                  activeAnnotationId={activeAnnotationId}
                  onAnnotationClick={(annotation) => {
                    handleAnnotationClick(annotation, 'sidebar');
                    setSidebarVisible(false);
                  }}
                  scrollToAnnotation={scrollToSidebarAnnotation}
                />
              </Drawer>
            )}
          </>
        )}
        </div>
      </div>
    </div>
  );
};

export default ChapterAnalysis;
