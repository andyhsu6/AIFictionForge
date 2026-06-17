import { Card, Button, Spin, Space, Tag, Typography, Alert, theme } from 'antd';
import { BookOutlined, RocketOutlined, BulbOutlined, UploadOutlined, DownloadOutlined, LoadingOutlined, CalendarOutlined, DeleteOutlined, CheckCircleOutlined, EditOutlined, PauseCircleOutlined, PictureOutlined, SwapOutlined, ReloadOutlined } from '@ant-design/icons';
import { useState } from 'react';
import type { ReactNode } from 'react';
import type { Project } from '../types';
import { bookshelfCardStyles, bookshelfCardHoverHandlers } from '../components/CardStyles';
import { useThemeMode } from '../theme/useThemeMode';

const { Paragraph } = Typography;

interface BookshelfPageProps {
  isMobile: boolean;
  loading: boolean;
  projects: Project[];
  showApiTip: boolean;
  setShowApiTip: (show: boolean) => void;
  exportableProjectsCount: number;
  onOpenImportModal: () => void;
  onOpenExportModal: () => void;
  onGoSettings: () => void;
  onStartWizard: () => void;
  onOpenInspiration: () => void;
  onEnterProject: (project: Project) => void;
  onDeleteProject: (projectId: string) => void;
  onGenerateCover: (project: Project, overwrite?: boolean) => void | Promise<void>;
  onDownloadCover: (project: Project) => void;
  formatWordCount: (count: number) => string;
  getProgress: (current: number, target: number) => number;
  getProgressColor: (progress: number) => string;
  getDisplayStatus: (status: string, progress: number) => string;
  getStatusTag: (status: string) => ReactNode;
  formatDate: (dateString: string) => string;
}

export default function BookshelfPage({
  isMobile,
  loading,
  projects,
  showApiTip,
  setShowApiTip,
  exportableProjectsCount,
  onOpenImportModal,
  onOpenExportModal,
  onGoSettings,
  onStartWizard,
  onOpenInspiration,
  onEnterProject,
  onDeleteProject,
  onGenerateCover,
  onDownloadCover,
  formatWordCount,
  getProgress,
  getProgressColor,
  getDisplayStatus,
  formatDate,
}: BookshelfPageProps) {
  const { token } = theme.useToken();
  const { resolvedMode } = useThemeMode();
  const isDark = resolvedMode === 'dark';
  const alphaColor = (color: string, alpha: number) => `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;
  const coverButtonStyle = {
    color: isDark ? token.colorWhite : token.colorPrimaryText,
    background: isDark
      ? 'rgba(255,255,255,0.14)'
      : `linear-gradient(135deg, ${alphaColor(token.colorWhite, 0.92)} 0%, ${alphaColor(token.colorBgContainer, 0.84)} 100%)`,
    borderColor: isDark ? 'rgba(255,255,255,0.22)' : alphaColor(token.colorPrimary, 0.35),
    boxShadow: isDark ? 'none' : `0 6px 16px ${alphaColor(token.colorPrimary, 0.18)}`,
    backdropFilter: 'blur(8px)',
  };
  const [flippedProjectIds, setFlippedProjectIds] = useState<Record<string, boolean>>({});
  const [coverGeneratingIds, setCoverGeneratingIds] = useState<Record<string, boolean>>({});
  const mobileBookHeight = 520;
  const desktopBookHeight = 520;
  const mobileSpineWidth = 32;

  const serialBookPalettes = [
    {
      spine: `linear-gradient(180deg, color-mix(in srgb, ${token.colorSuccess} ${isDark ? 58 : 42}%, ${token.colorBgContainer} ${isDark ? 42 : 58}%) 0%, color-mix(in srgb, ${token.colorSuccess} ${isDark ? 74 : 56}%, ${token.colorText} ${isDark ? 26 : 44}%) 100%)`,
      spineBorder: `color-mix(in srgb, ${token.colorSuccess} ${isDark ? 66 : 52}%, ${token.colorBorder} ${isDark ? 34 : 48}%)`,
      ribbon: `color-mix(in srgb, ${token.colorSuccess} ${isDark ? 70 : 82}%, ${token.colorPrimary} ${isDark ? 30 : 18}%)`,
    },
    {
      spine: `linear-gradient(180deg, color-mix(in srgb, ${token.colorWarning} ${isDark ? 64 : 52}%, ${token.colorBgContainer} ${isDark ? 36 : 48}%) 0%, color-mix(in srgb, ${token.colorWarning} ${isDark ? 80 : 66}%, ${token.colorText} ${isDark ? 20 : 34}%) 100%)`,
      spineBorder: `color-mix(in srgb, ${token.colorWarning} ${isDark ? 70 : 56}%, ${token.colorBorder} ${isDark ? 30 : 44}%)`,
      ribbon: `color-mix(in srgb, ${token.colorWarning} ${isDark ? 72 : 82}%, ${token.colorPrimary} ${isDark ? 28 : 18}%)`,
    },
    {
      spine: `linear-gradient(180deg, color-mix(in srgb, ${token.colorInfo} ${isDark ? 46 : 30}%, ${token.colorText} ${isDark ? 54 : 70}%) 0%, color-mix(in srgb, ${token.colorText} ${isDark ? 66 : 52}%, ${token.colorBgContainer} ${isDark ? 34 : 48}%) 100%)`,
      spineBorder: `color-mix(in srgb, ${token.colorText} ${isDark ? 74 : 64}%, ${token.colorBorder} ${isDark ? 26 : 36}%)`,
      ribbon: `color-mix(in srgb, ${token.colorInfo} ${isDark ? 68 : 76}%, ${token.colorPrimary} ${isDark ? 32 : 24}%)`,
    },
    {
      spine: `linear-gradient(180deg, color-mix(in srgb, ${token.colorPrimary} ${isDark ? 62 : 50}%, ${token.colorBgContainer} ${isDark ? 38 : 50}%) 0%, color-mix(in srgb, ${token.colorPrimary} ${isDark ? 78 : 62}%, ${token.colorText} ${isDark ? 22 : 38}%) 100%)`,
      spineBorder: `color-mix(in srgb, ${token.colorPrimary} ${isDark ? 70 : 58}%, ${token.colorBorder} ${isDark ? 30 : 42}%)`,
      ribbon: `color-mix(in srgb, ${token.colorPrimary} ${isDark ? 74 : 86}%, ${token.colorInfo} ${isDark ? 26 : 14}%)`,
    },
  ];

  const completedBookPalette = {
    spine: `linear-gradient(180deg, color-mix(in srgb, ${token.colorPrimary} ${isDark ? 68 : 52}%, ${token.colorSuccess} ${isDark ? 32 : 48}%) 0%, color-mix(in srgb, ${token.colorPrimary} ${isDark ? 82 : 66}%, ${token.colorText} ${isDark ? 18 : 34}%) 100%)`,
    spineBorder: `color-mix(in srgb, ${token.colorPrimary} ${isDark ? 76 : 62}%, ${token.colorBorder} ${isDark ? 24 : 38}%)`,
    ribbon: `color-mix(in srgb, ${token.colorPrimary} ${isDark ? 62 : 48}%, ${token.colorError} ${isDark ? 38 : 52}%)`,
  };

  const getRibbonStatusIcon = (displayStatus: string, isWizardIncomplete: boolean, isCompleted: boolean) => {
    const commonStyle = { color: token.colorWhite, fontSize: isMobile ? 12 : 14 };

    if (isWizardIncomplete) {
      return <LoadingOutlined spin style={commonStyle} />;
    }
    if (isCompleted) {
      return <CheckCircleOutlined style={commonStyle} />;
    }
    if (displayStatus.includes('暂停') || displayStatus.includes('搁置')) {
      return <PauseCircleOutlined style={commonStyle} />;
    }
    if (displayStatus.includes('筹备') || displayStatus.includes('准备') || displayStatus.includes('大纲')) {
      return <BulbOutlined style={commonStyle} />;
    }

    return <EditOutlined style={commonStyle} />;
  };

  const toggleProjectFace = (projectId: string) => {
    setFlippedProjectIds((prev) => ({ ...prev, [projectId]: !prev[projectId] }));
  };

  const handleGenerateCoverClick = async (event: React.MouseEvent<HTMLElement>, project: Project, overwrite: boolean = true) => {
    event.stopPropagation();

    if (coverGeneratingIds[project.id]) {
      return;
    }

    setCoverGeneratingIds((prev) => ({ ...prev, [project.id]: true }));

    try {
      await onGenerateCover(project, overwrite);
    } finally {
      setCoverGeneratingIds((prev) => ({ ...prev, [project.id]: false }));
    }
  };

  return (
    <div>
      <Card
        variant="borderless"
        style={{
          marginBottom: isMobile ? 12 : 16,
          borderRadius: isMobile ? 14 : 18,
          background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`,
          boxShadow: token.boxShadowSecondary,
          border: 'none',
          position: 'relative',
          overflow: 'hidden',
        }}
        styles={{ body: { padding: isMobile ? '14px 14px' : '16px 18px' } }}
      >
        <div style={{ position: 'absolute', top: -44, right: -44, width: 150, height: 150, borderRadius: '50%', background: token.colorWhite, opacity: 0.1, pointerEvents: 'none' }} />
        <div style={{ position: 'absolute', bottom: -36, left: '26%', width: 100, height: 100, borderRadius: '50%', background: token.colorWhite, opacity: 0.06, pointerEvents: 'none' }} />

        <div
          style={{
            position: 'relative',
            zIndex: 1,
            display: 'flex',
            flexDirection: isMobile ? 'column' : 'row',
            alignItems: isMobile ? 'stretch' : 'center',
            justifyContent: 'space-between',
            gap: isMobile ? 12 : 16,
          }}
        >
          <div>
            <div
              style={{
                fontSize: isMobile ? 18 : 22,
                fontWeight: 700,
                color: token.colorWhite,
                lineHeight: 1.3,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                textShadow: `0 2px 4px ${alphaColor(token.colorText, 0.2)}`,
              }}
            >
              <BookOutlined style={{ opacity: 0.92 }} />
              我的书架
            </div>
            <div
              style={{
                marginTop: 4,
                fontSize: isMobile ? 12 : 13,
                color: alphaColor(token.colorWhite, 0.9),
              }}
            >
              点击书本即可进入项目，统一查看进度、字数与状态。
            </div>
          </div>

          <Space size={8} wrap>
            <Button
              icon={<UploadOutlined />}
              onClick={onOpenImportModal}
              style={{ borderRadius: 10 }}
            >
              导入项目
            </Button>
            <Button
              icon={<DownloadOutlined />}
              onClick={onOpenExportModal}
              disabled={exportableProjectsCount === 0}
              style={{ borderRadius: 10 }}
            >
              导出项目
            </Button>
          </Space>
        </div>
      </Card>

      {showApiTip && projects.length === 0 && (
        <Alert
          message="欢迎使用 MuMuAINovel"
          description={
            <div style={{
              display: 'flex',
              flexDirection: isMobile ? 'column' : 'row',
              alignItems: isMobile ? 'flex-start' : 'center',
              gap: isMobile ? 12 : 16,
              justifyContent: 'space-between'
            }}>
              <span style={{ fontSize: isMobile ? 12 : 14 }}>
                在开始创作之前，请先配置您的AI接口（支持 OpenAI / Anthropic）。
              </span>
              <Button
                size="small"
                type="primary"
                onClick={onGoSettings}
                style={{ flexShrink: 0 }}
              >
                去配置
              </Button>
            </div>
          }
          type="info"
          showIcon
          closable
          onClose={() => setShowApiTip(false)}
          style={{
            marginBottom: isMobile ? 16 : 24,
            borderRadius: 12
          }}
        />
      )}

      <Spin spinning={loading}>
        <div style={{
          ...bookshelfCardStyles.container,
          borderRadius: isMobile ? 12 : 16,
          border: `1px solid ${isDark ? alphaColor(token.colorBorder, 0.42) : alphaColor(token.colorText, 0.06)}`,
          backgroundColor: isDark ? alphaColor(token.colorBgContainer, 0.42) : alphaColor(token.colorBgContainer, 0.72),
          backgroundImage: `radial-gradient(${alphaColor(token.colorText, isDark ? 0.16 : 0.08)} 1px, transparent 0)`,
          backgroundSize: '18px 18px',
          boxShadow: `inset 0 1px 0 ${alphaColor(token.colorWhite, isDark ? 0.08 : 0.45)}`,
          padding: isMobile ? '12px' : '18px',
          ...(isMobile && {
            gridTemplateColumns: '1fr',
            gap: '14px',
          })
        }}>
          <div style={{ position: 'relative', width: '100%', minWidth: 0, minHeight: isMobile ? mobileBookHeight : desktopBookHeight }}>
            <Card
              hoverable
              style={{ ...bookshelfCardStyles.newProjectCard, minHeight: isMobile ? mobileBookHeight : desktopBookHeight }}
              styles={{ body: { padding: 0, flex: 1, display: 'flex', flexDirection: 'column' } }}
              {...bookshelfCardHoverHandlers}
              data-type="new-project"
              data-card-style="bookshelf-book"
              data-book-kind="new"
            >
              <div style={{
                position: 'absolute',
                inset: 0,
                background: `linear-gradient(180deg, ${alphaColor(isDark ? token.colorBgContainer : token.colorWhite, isDark ? 0.14 : 0.42)} 0%, transparent 22%, transparent 78%, ${alphaColor(token.colorWarning, isDark ? 0.1 : 0.05)} 100%)`,
                pointerEvents: 'none'
              }} />
              <div style={{
                width: '100%',
                minHeight: isMobile ? mobileBookHeight : desktopBookHeight,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: isMobile ? 18 : 22,
                padding: isMobile ? '24px 18px 64px' : '34px 28px 72px',
                position: 'relative',
                zIndex: 1,
              }}>
                <div style={{
                  width: isMobile ? 56 : 68,
                  height: isMobile ? 56 : 68,
                  borderRadius: '50%',
                  display: 'grid',
                  placeItems: 'center',
                  background: `linear-gradient(180deg, ${alphaColor(token.colorPrimary, isDark ? 0.18 : 0.12)} 0%, ${alphaColor(token.colorPrimary, isDark ? 0.28 : 0.2)} 100%)`,
                  color: token.colorPrimary,
                  boxShadow: `inset 0 1px 0 ${alphaColor(token.colorWhite, isDark ? 0.18 : 0.75)}`,
                }}>
                  <BookOutlined style={{ fontSize: isMobile ? 24 : 28 }} />
                </div>

                <div style={{ width: '100%', maxWidth: 240, display: 'flex', flexDirection: 'column', gap: isMobile ? 10 : 14 }}>
                  <Button
                    type="primary"
                    size={isMobile ? 'middle' : 'large'}
                    icon={<RocketOutlined />}
                    onClick={onStartWizard}
                    style={{
                      height: isMobile ? 42 : 52,
                      fontSize: isMobile ? 14 : 16,
                      borderRadius: 10,
                      boxShadow: `0 10px 18px ${alphaColor(token.colorPrimary, isDark ? 0.14 : 0.22)}`,
                    }}
                    block
                  >
                    快速开始
                  </Button>
                  <Button
                    size={isMobile ? 'middle' : 'large'}
                    icon={<BulbOutlined />}
                    onClick={onOpenInspiration}
                    style={{
                      height: isMobile ? 42 : 52,
                      fontSize: isMobile ? '14px' : '16px',
                      borderRadius: 10,
                      borderColor: alphaColor(token.colorWarning, isDark ? 0.34 : 0.5),
                      color: `color-mix(in srgb, ${token.colorWarning} ${isDark ? 78 : 72}%, ${token.colorText} ${isDark ? 22 : 28}%)`,
                      background: `linear-gradient(180deg, ${alphaColor(token.colorWarning, isDark ? 0.12 : 0.12)} 0%, ${alphaColor(token.colorWarning, isDark ? 0.2 : 0.2)} 100%)`,
                    }}
                    block
                  >
                    灵感模式
                  </Button>
                </div>

                <div style={{
                  position: 'absolute',
                  bottom: isMobile ? 24 : 28,
                  left: 24,
                  right: 24,
                  textAlign: 'center',
                  fontSize: isMobile ? 11 : 12,
                  color: token.colorTextTertiary,
                  letterSpacing: 0.5,
                }}>
                  开始一个新的创作旅程
                </div>
              </div>
            </Card>
          </div>

          {Array.isArray(projects) && projects.map((project, index) => {
            const progress = getProgress(project.current_words || 0, project.target_words || 0);
            const progressColor = getProgressColor(progress);
            const isWizardIncomplete = project.wizard_status === 'incomplete';
            const displayStatus = getDisplayStatus(project.status, progress);
            const isCompleted = progress >= 100 || displayStatus.includes('完结');
            const palette = isCompleted ? completedBookPalette : serialBookPalettes[index % serialBookPalettes.length];
            const tags = project.genre ? project.genre.split(/[,、，]/).map((t: string) => t.trim()).filter((t: string) => t) : [];

            const ribbonStatusIcon = getRibbonStatusIcon(displayStatus, isWizardIncomplete, isCompleted);

            const isFlipped = !!flippedProjectIds[project.id];
            const coverActionLoading = !!coverGeneratingIds[project.id];
            const coverReady = project.cover_status === 'ready' && !!project.cover_image_url;
            const coverGenerating = project.cover_status === 'generating' || coverActionLoading;
            const coverFailed = project.cover_status === 'failed' && !coverActionLoading;
            const showCoverFace = coverReady && !isFlipped;

            return (
              <div key={project.id} style={{ position: 'relative', width: '100%', minWidth: 0, height: isMobile ? mobileBookHeight : desktopBookHeight }}>
                <Card
                  hoverable
                  style={{ ...bookshelfCardStyles.projectCard, height: isMobile ? mobileBookHeight : desktopBookHeight }}
                  styles={{ body: { padding: 0, height: '100%', flex: 1, display: 'flex', flexDirection: 'column' } }}
                  {...bookshelfCardHoverHandlers}
                  onClick={() => !isFlipped && onEnterProject(project)}
                  data-card-style="bookshelf-book"
                  data-book-kind="project"
                >
                  <div style={{
                    position: 'absolute',
                    left: 0,
                    top: 0,
                    bottom: 0,
                    width: isMobile ? mobileSpineWidth : 22,
                    borderRadius: '3px 0 0 3px',
                    background: palette.spine,
                    borderRight: `1px solid ${palette.spineBorder}`,
                    boxShadow: `inset -3px 0 6px ${alphaColor(token.colorText, isDark ? 0.42 : 0.25)}, inset 2px 0 4px ${alphaColor(token.colorWhite, isDark ? 0.08 : 0.14)}`,
                    zIndex: 0,
                    pointerEvents: 'none',
                  }} />
                  <div style={{
                    position: 'absolute',
                    top: 0,
                    right: isMobile ? 16 : 24,
                    width: isMobile ? 24 : 30,
                    height: isMobile ? 42 : 52,
                    clipPath: 'polygon(0 0, 100% 0, 100% 100%, 50% calc(100% - 7px), 0 100%)',
                    background: palette.ribbon,
                    boxShadow: `0 8px 16px ${alphaColor(token.colorText, isDark ? 0.26 : 0.14)}`,
                    display: 'flex',
                    alignItems: 'flex-start',
                    justifyContent: 'center',
                    paddingTop: isMobile ? 8 : 10,
                    zIndex: 3,
                    pointerEvents: 'none',
                  }}>
                    {ribbonStatusIcon}
                  </div>

                  <div style={{
                    position: 'relative',
                    zIndex: 2,
                    display: 'flex',
                    flexDirection: 'column',
                    flex: 1,
                    height: '100%',
                    padding: isMobile ? '16px 16px 14px 38px' : '20px 20px 18px 42px',
                    overflow: 'hidden',
                    width: '100%',
                    boxSizing: 'border-box',
                  }}>
                    {showCoverFace && (
                      <>
                        <div style={{
                          position: 'absolute',
                          inset: 0,
                          backgroundImage: `url(${project.cover_image_url})`,
                          backgroundSize: 'cover',
                          backgroundPosition: 'center',
                          backgroundRepeat: 'no-repeat',
                        }} />
                        <div style={{
                          position: 'absolute',
                          inset: 0,
                          background: isDark
                            ? 'linear-gradient(180deg, rgba(7,10,18,0.18) 0%, rgba(7,10,18,0.38) 24%, rgba(7,10,18,0.72) 62%, rgba(7,10,18,0.88) 100%)'
                            : 'linear-gradient(180deg, rgba(255,255,255,0.08) 0%, rgba(16,24,40,0.22) 24%, rgba(16,24,40,0.62) 62%, rgba(16,24,40,0.82) 100%)',
                        }} />
                      </>
                    )}

                    <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 8, position: 'relative', zIndex: 2 }}>
                      <Button
                        type="text"
                        size="small"
                        icon={showCoverFace ? <BookOutlined /> : <SwapOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          toggleProjectFace(project.id);
                        }}
                        style={showCoverFace ? {
                          color: token.colorWhite,
                          borderRadius: 999,
                          background: 'rgba(255,255,255,0.18)',
                          backdropFilter: 'blur(10px)',
                          border: '1px solid rgba(255,255,255,0.22)',
                        } : undefined}
                      >
                        {showCoverFace ? '切换回详情' : (coverReady ? '查看封面' : '封面操作')}
                      </Button>
                    </div>

                    {showCoverFace ? (
                      <div style={{
                        position: 'relative',
                        zIndex: 2,
                        flex: 1,
                        display: 'flex',
                        flexDirection: 'column',
                        minHeight: 0,
                        width: '100%',
                      }}>
                        <div style={{
                          minHeight: isMobile ? 82 : 90,
                          width: '100%',
                          marginBottom: isMobile ? 10 : 12,
                        }}>
                          <div style={{
                            fontSize: isMobile ? 18 : 22,
                            fontWeight: 700,
                            color: token.colorWhite,
                            lineHeight: 1.3,
                            display: '-webkit-box',
                            WebkitLineClamp: 2,
                            WebkitBoxOrient: 'vertical',
                            overflow: 'hidden',
                            wordBreak: 'break-word',
                            fontFamily: 'Georgia, "Times New Roman", "Noto Serif SC", serif',
                            textShadow: '0 2px 12px rgba(0,0,0,0.35)',
                          }}>
                            {project.title}
                          </div>
                          <div style={{
                            display: 'flex',
                            flexWrap: 'wrap',
                            gap: 6,
                            marginTop: 10,
                            minHeight: isMobile ? 20 : 22,
                            alignItems: 'flex-start',
                          }}>
                            {tags.length > 0 ? tags.slice(0, 3).map((tag: string, idx: number) => (
                              <Tag key={idx} style={{
                                margin: 0,
                                padding: isMobile ? '0 7px' : '0 8px',
                                borderRadius: 999,
                                border: '1px solid rgba(255,255,255,0.24)',
                                background: 'rgba(255,255,255,0.14)',
                                color: token.colorWhite,
                                fontSize: isMobile ? 10 : 11,
                                lineHeight: isMobile ? '18px' : '20px',
                                fontWeight: 500,
                                backdropFilter: 'blur(8px)',
                              }}>
                                {tag}
                              </Tag>
                            )) : (
                              <Tag style={{
                                margin: 0,
                                padding: isMobile ? '0 7px' : '0 8px',
                                borderRadius: 999,
                                border: '1px solid rgba(255,255,255,0.24)',
                                background: 'rgba(255,255,255,0.14)',
                                color: token.colorWhite,
                                fontSize: isMobile ? 10 : 11,
                                lineHeight: isMobile ? '18px' : '20px',
                                fontWeight: 500,
                                backdropFilter: 'blur(8px)',
                              }}>
                                未分类
                              </Tag>
                            )}
                          </div>
                        </div>

                        <div style={{ flex: 1, minHeight: isMobile ? 54 : 72 }} />

                        <div style={{
                          display: 'flex',
                          flexDirection: 'column',
                          gap: 10,
                          width: '100%',
                        }}>
                          <div style={{
                            padding: isMobile ? '12px 10px' : '14px 12px',
                            borderRadius: 10,
                            background: 'rgba(10,18,32,0.34)',
                            border: '1px solid rgba(255,255,255,0.16)',
                            boxShadow: '0 8px 24px rgba(0,0,0,0.18)',
                            backdropFilter: 'blur(12px)',
                            minHeight: isMobile ? 74 : 82,
                          }}>
                            <div style={{
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                              marginBottom: 6,
                              fontSize: isMobile ? 11 : 12,
                              color: 'rgba(255,255,255,0.82)',
                            }}>
                              <span>完成进度</span>
                              <span style={{ color: token.colorWhite, fontWeight: 700 }}>{progress}%</span>
                            </div>
                            <div style={{
                              height: 6,
                              width: '100%',
                              borderRadius: 999,
                              overflow: 'hidden',
                              background: 'rgba(255,255,255,0.18)',
                              marginBottom: 12,
                            }}>
                              <div style={{
                                width: `${progress}%`,
                                height: '100%',
                                borderRadius: 999,
                                background: progressColor,
                                transition: 'width 0.3s ease',
                              }} />
                            </div>
                            <div style={{ display: 'flex', alignItems: 'stretch', textAlign: 'center' }}>
                              <div style={{ flex: 1 }}>
                                <div style={{
                                  fontSize: isMobile ? 22 : 26,
                                  fontWeight: 700,
                                  color: token.colorWhite,
                                  lineHeight: 1.1,
                                  fontFamily: 'Georgia, "Times New Roman", serif',
                                }}>
                                  {formatWordCount(project.current_words || 0)}
                                </div>
                                <div style={{
                                  fontSize: isMobile ? 10 : 11,
                                  color: 'rgba(255,255,255,0.72)',
                                  marginTop: 4,
                                }}>
                                  已写字数
                                </div>
                              </div>
                              <div style={{
                                width: 1,
                                margin: '0 12px',
                                background: 'rgba(255,255,255,0.16)',
                              }} />
                              <div style={{ flex: 1 }}>
                                <div style={{
                                  fontSize: isMobile ? 22 : 26,
                                  fontWeight: 700,
                                  color: progress >= 100 ? '#7CFFB2' : token.colorWhite,
                                  lineHeight: 1.1,
                                  fontFamily: 'Georgia, "Times New Roman", serif',
                                }}>
                                  {formatWordCount(project.target_words || 0)}
                                </div>
                                <div style={{
                                  fontSize: isMobile ? 10 : 11,
                                  color: 'rgba(255,255,255,0.72)',
                                  marginTop: 4,
                                }}>
                                  目标字数
                                </div>
                              </div>
                            </div>
                          </div>

                          <div style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            gap: 10,
                            flexWrap: 'wrap',
                            paddingTop: isMobile ? 10 : 12,
                            borderTop: '1px solid rgba(255,255,255,0.18)',
                            minHeight: isMobile ? 42 : 46,
                          }}>
                            <Space size={4} style={{ fontSize: isMobile ? 11 : 12, color: 'rgba(255,255,255,0.76)' }}>
                              <CalendarOutlined style={{ fontSize: isMobile ? 10 : 12 }} />
                              {formatDate(project.updated_at)}
                            </Space>

                            <Space wrap>
                              <Button
                                size="small"
                                icon={<DownloadOutlined />}
                                onClick={(e) => { e.stopPropagation(); onDownloadCover(project); }}
                                disabled={coverActionLoading}
                                style={{
                                  color: token.colorWhite,
                                  background: 'rgba(255,255,255,0.14)',
                                  borderColor: 'rgba(255,255,255,0.22)',
                                  backdropFilter: 'blur(8px)',
                                }}
                              >
                                下载封面
                              </Button>
                              <Button
                                size="small"
                                icon={<ReloadOutlined />}
                                loading={coverActionLoading}
                                onClick={(e) => void handleGenerateCoverClick(e, project, true)}
                                style={{
                                  color: token.colorWhite,
                                  background: 'rgba(255,255,255,0.14)',
                                  borderColor: 'rgba(255,255,255,0.22)',
                                  backdropFilter: 'blur(8px)',
                                }}
                              >
                                {coverActionLoading ? '重新生成中...' : '重新生成'}
                              </Button>
                            </Space>
                          </div>
                        </div>
                      </div>
                    ) : (
                      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 12, justifyContent: 'space-between', minHeight: 0, width: '100%' }}>
                        {isFlipped && coverGenerating ? (
                          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12, minHeight: 0, width: '100%' }}>
                            <LoadingOutlined spin style={{ fontSize: 28, color: token.colorPrimary }} />
                            <div style={{ color: token.colorTextSecondary }}>封面生成中，请稍候...</div>
                          </div>
                        ) : isFlipped && coverFailed ? (
                          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12, textAlign: 'center', minHeight: 0 }}>
                            <PictureOutlined style={{ fontSize: 36, color: token.colorTextTertiary }} />
                            <div style={{ color: token.colorError }}>封面生成失败</div>
                            <div style={{ color: token.colorTextSecondary, fontSize: 12 }}>{project.cover_error || '请稍后重试'}</div>
                            <Button
                              type="primary"
                              icon={<ReloadOutlined />}
                              loading={coverActionLoading}
                              onClick={(e) => void handleGenerateCoverClick(e, project, true)}
                              style={coverButtonStyle}
                            >
                              {coverActionLoading ? '重新生成中...' : '重新生成'}
                            </Button>
                          </div>
                        ) : isFlipped && !coverReady ? (
                          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12, textAlign: 'center', minHeight: 0 }}>
                            <PictureOutlined style={{ fontSize: 36, color: token.colorTextTertiary }} />
                            <div style={{ color: token.colorTextSecondary }}>当前暂无可用封面</div>
                            <Button
                              type="primary"
                              icon={<PictureOutlined />}
                              loading={coverActionLoading}
                              onClick={(e) => void handleGenerateCoverClick(e, project, true)}
                              style={coverButtonStyle}
                            >
                              {coverActionLoading ? '生成中...' : '生成封面'}
                            </Button>
                          </div>
                        ) : (
                          <>
                            <div style={{ marginBottom: isMobile ? 10 : 12, paddingRight: isMobile ? 18 : 30 }}>
                              <div style={{
                                display: 'flex',
                                alignItems: 'flex-start',
                                gap: 8,
                                marginBottom: isMobile ? 8 : 10,
                                minHeight: isMobile ? 50 : 58,
                              }}>
                                <BookOutlined style={{
                                  fontSize: isMobile ? 14 : 16,
                                  color: alphaColor(token.colorText, 0.4),
                                  marginTop: 2,
                                  flexShrink: 0,
                                }} />
                                <div style={{
                                  fontSize: isMobile ? 18 : 22,
                                  fontWeight: 700,
                                  color: token.colorText,
                                  lineHeight: 1.3,
                                  display: '-webkit-box',
                                  WebkitLineClamp: 2,
                                  WebkitBoxOrient: 'vertical',
                                  overflow: 'hidden',
                                  wordBreak: 'break-word',
                                  fontFamily: 'Georgia, "Times New Roman", "Noto Serif SC", serif',
                                }}>
                                  {project.title}
                                </div>
                              </div>

                              <div style={{
                                display: 'flex',
                                flexWrap: 'wrap',
                                gap: 6,
                                minHeight: isMobile ? 20 : 22,
                                alignItems: 'flex-start',
                              }}>
                                {tags.length > 0 ? tags.slice(0, 3).map((tag: string, idx: number) => (
                                  <Tag key={idx} style={{
                                    margin: 0,
                                    padding: isMobile ? '0 7px' : '0 8px',
                                    borderRadius: 4,
                                    border: `1px solid ${alphaColor(token.colorSuccess, 0.18)}`,
                                    background: alphaColor(token.colorSuccess, 0.08),
                                    color: token.colorSuccess,
                                    fontSize: isMobile ? 10 : 11,
                                    lineHeight: isMobile ? '18px' : '20px',
                                    fontWeight: 500,
                                  }}>
                                    {tag}
                                  </Tag>
                                )) : (
                                  <Tag style={{
                                    margin: 0,
                                    padding: isMobile ? '0 7px' : '0 8px',
                                    borderRadius: 4,
                                    border: `1px solid ${alphaColor(token.colorSuccess, 0.18)}`,
                                    background: alphaColor(token.colorSuccess, 0.08),
                                    color: token.colorSuccess,
                                    fontSize: isMobile ? 10 : 11,
                                    lineHeight: isMobile ? '18px' : '20px',
                                    fontWeight: 500,
                                  }}>
                                    未分类
                                  </Tag>
                                )}
                              </div>
                            </div>

                            <Paragraph
                              ellipsis={{ rows: isMobile ? 3 : 3 }}
                              style={{
                                fontSize: isMobile ? 12 : 13,
                                color: token.colorTextSecondary,
                                marginBottom: isMobile ? 12 : 16,
                                lineHeight: 1.7,
                                flexGrow: 1,
                              }}
                            >
                              {project.description || '暂无描述...'}
                            </Paragraph>

                            <div style={{ marginBottom: isMobile ? 14 : 18 }}>
                              <div style={{
                                display: 'flex',
                                justifyContent: 'space-between',
                                alignItems: 'center',
                                marginBottom: 6,
                                fontSize: isMobile ? 11 : 12,
                              }}>
                                <span style={{ color: token.colorTextTertiary }}>完成进度</span>
                                <span style={{ color: progressColor, fontWeight: 700 }}>{progress}%</span>
                              </div>
                              <div style={{
                                height: 6,
                                width: '100%',
                                borderRadius: 999,
                                overflow: 'hidden',
                                background: alphaColor(token.colorText, 0.06),
                              }}>
                                <div style={{
                                  width: `${progress}%`,
                                  height: '100%',
                                  borderRadius: 999,
                                  background: progressColor,
                                  transition: 'width 0.3s ease',
                                }} />
                              </div>
                            </div>

                            <div style={{
                              marginBottom: isMobile ? 12 : 16,
                              padding: isMobile ? '12px 10px' : '14px 12px',
                              background: `linear-gradient(180deg, ${alphaColor(token.colorBgContainer, 0.94)} 0%, ${alphaColor(token.colorFillSecondary, 0.78)} 100%)`,
                              borderRadius: 10,
                              border: `1px solid ${alphaColor(token.colorText, 0.06)}`,
                              boxShadow: `inset 0 1px 2px ${alphaColor(token.colorText, 0.08)}`,
                            }}>
                              <div style={{ display: 'flex', alignItems: 'stretch', textAlign: 'center' }}>
                                <div style={{ flex: 1 }}>
                                  <div style={{
                                    fontSize: isMobile ? 22 : 26,
                                    fontWeight: 700,
                                    color: token.colorText,
                                    lineHeight: 1.1,
                                    fontFamily: 'Georgia, "Times New Roman", serif',
                                  }}>
                                    {formatWordCount(project.current_words || 0)}
                                  </div>
                                  <div style={{
                                    fontSize: isMobile ? 10 : 11,
                                    color: token.colorTextTertiary,
                                    marginTop: 4,
                                  }}>
                                    已写字数
                                  </div>
                                </div>
                                <div style={{
                                  width: 1,
                                  margin: '0 12px',
                                  background: alphaColor(token.colorText, 0.1),
                                }} />
                                <div style={{ flex: 1 }}>
                                  <div style={{
                                    fontSize: isMobile ? 22 : 26,
                                    fontWeight: 700,
                                    color: progress >= 100 ? token.colorSuccess : progressColor,
                                    lineHeight: 1.1,
                                    fontFamily: 'Georgia, "Times New Roman", serif',
                                  }}>
                                    {formatWordCount(project.target_words || 0)}
                                  </div>
                                  <div style={{
                                    fontSize: isMobile ? 10 : 11,
                                    color: token.colorTextTertiary,
                                    marginTop: 4,
                                  }}>
                                    目标字数
                                  </div>
                                </div>
                              </div>
                            </div>

                            <div style={{
                              display: 'grid',
                              gridTemplateColumns: '1fr auto 1fr',
                              alignItems: 'center',
                              gap: 10,
                              paddingTop: isMobile ? 10 : 12,
                              borderTop: `1px solid ${alphaColor(token.colorText, 0.06)}`,
                              color: token.colorTextTertiary,
                              marginTop: 'auto',
                            }}>
                              <Space size={4} style={{ fontSize: isMobile ? 11 : 12, color: token.colorTextTertiary, justifySelf: 'start' }}>
                                <CalendarOutlined style={{ fontSize: isMobile ? 10 : 12 }} />
                                {formatDate(project.updated_at)}
                              </Space>

                              <div style={{ justifySelf: 'center' }}>
                                {!coverReady && (
                                  <Button
                                    size="small"
                                    icon={<PictureOutlined />}
                                    loading={coverActionLoading}
                                    onClick={(e) => void handleGenerateCoverClick(e, project, true)}
                                    style={{
                                      ...coverButtonStyle,
                                      minWidth: isMobile ? 112 : 124,
                                    }}
                                  >
                                    {coverActionLoading ? '生成中...' : '生成封面'}
                                  </Button>
                                )}
                              </div>

                              <div style={{ justifySelf: 'end' }}>
                                <Button
                                  type="text"
                                  size="small"
                                  danger
                                  icon={<DeleteOutlined style={{ fontSize: isMobile ? 12 : 14 }} />}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    onDeleteProject(project.id);
                                  }}
                                  style={{
                                    padding: isMobile ? '2px 4px' : '4px 8px',
                                    borderRadius: 8,
                                  }}
                                />
                              </div>
                            </div>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                </Card>
              </div>
            );
          })}
        </div>
      </Spin>
    </div>
  );
}
