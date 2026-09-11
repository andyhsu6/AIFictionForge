import React, { useState, useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { App, Modal, Input, Button, Space, Radio, Segmented, InputNumber, Card, Alert, Spin, Typography, Divider, theme } from 'antd';
import { ThunderboltOutlined, CheckOutlined, ReloadOutlined, EditOutlined, LoadingOutlined, PauseCircleOutlined } from '@ant-design/icons';
import { chapterApi } from '../services/api';

const { TextArea } = Input;
const { Text, Paragraph } = Typography;

interface PartialRegenerateModalProps {
  visible: boolean;
  chapterId: string;
  selectedText: string;
  startPosition: number;
  endPosition: number;
  styleId?: number;
  mode?: RegenerateMode;
  onClose: () => void;
  onApply: (newText: string, startPosition: number, endPosition: number, mode: RegenerateMode) => void;
}

type LengthMode = 'similar' | 'expand' | 'condense' | 'custom';
export type RegenerateMode = 'rewrite' | 'continue';

// 续写为客户端分段循环（后端是无状态单段原语）：
// 目标字数超过软阈值时先确认；每段约按此字数估算段数。
const SOFT_TARGET_THRESHOLD = 50000;
const ESTIMATED_SEGMENT_CHARS = 8000;
// 滚动上下文只回传末尾若干字符，控制请求体大小
const ROLLING_CONTEXT_CHARS = 4000;

/** 单段 SSE result 事件里续写相关的返回字段 */
interface ContinueSegmentResult {
  new_text?: string;
  mode?: RegenerateMode;
  content_hash?: string;
  segment_index?: number;
  segment_count?: number;
  requested_chars?: number;
  generated_chars?: number;
  complete?: boolean;
}

/** 取字符串末尾 n 个字符（不足 n 时原样返回） */
const tail = (s: string, n: number): string => (s.length > n ? s.slice(s.length - n) : s);

/**
 * 局部重写/续写弹窗组件
 * rewrite：配置并执行选中文本的AI重写（单次请求、替换原文）
 * continue：以选中文本为锚点，客户端分段循环生成续写并一次性插入（不替换后续文本）
 */
export const PartialRegenerateModal: React.FC<PartialRegenerateModalProps> = ({
  visible,
  chapterId,
  selectedText,
  startPosition,
  endPosition,
  styleId,
  mode: initialMode = 'rewrite',
  onClose,
  onApply,
}) => {
  const { message, modal } = App.useApp();
  const { t } = useTranslation('partialRegenerateModal');
  const { token } = theme.useToken();
  const [mode, setMode] = useState<RegenerateMode>(initialMode);
  const [userInstructions, setUserInstructions] = useState('');
  const [lengthMode, setLengthMode] = useState<LengthMode>('similar');
  const [customWordCount, setCustomWordCount] = useState<number>(selectedText.length);
  const [continueTarget, setContinueTarget] = useState<number>(1000);
  const [isGenerating, setIsGenerating] = useState(false);
  const [generatedText, setGeneratedText] = useState('');
  const [hasGenerated, setHasGenerated] = useState(false);
  const [progress, setProgress] = useState(0);
  const [progressMessage, setProgressMessage] = useState('');
  // 续写分段状态
  const [segments, setSegments] = useState<string[]>([]);
  const [currentSegment, setCurrentSegment] = useState(0);
  const [totalSegments, setTotalSegments] = useState<number | null>(null);
  const [contentHash, setContentHash] = useState<string | null>(null);
  const [resumePending, setResumePending] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const generatedTextRef = useRef<HTMLDivElement>(null);
  const errorNotifiedRef = useRef(false);
  const segmentsRef = useRef<string[]>([]);
  const contentHashRef = useRef<string | null>(null);
  const totalSegmentsRef = useRef<number | null>(null);
  const stopRequestedRef = useRef(false);


  // 重置状态
  useEffect(() => {
    if (visible) {
      setMode(initialMode);
      setUserInstructions('');
      setLengthMode('similar');
      setCustomWordCount(selectedText.length);
      setContinueTarget(1000);
      setIsGenerating(false);
      setGeneratedText('');
      setHasGenerated(false);
      setProgress(0);
      setProgressMessage('');
      setSegments([]);
      segmentsRef.current = [];
      setCurrentSegment(0);
      setTotalSegments(null);
      totalSegmentsRef.current = null;
      setContentHash(null);
      contentHashRef.current = null;
      setResumePending(false);
      stopRequestedRef.current = false;
    }
  }, [visible, initialMode, selectedText.length]);

  // 自动滚动到底部
  useEffect(() => {
    if (generatedTextRef.current && isGenerating) {
      generatedTextRef.current.scrollTop = generatedTextRef.current.scrollHeight;
    }
  }, [generatedText, isGenerating]);

  const handleModeChange = (next: RegenerateMode) => {
    if (isGenerating || next === mode) {
      return;
    }
    setMode(next);
    setGeneratedText('');
    setHasGenerated(false);
    setProgress(0);
    setProgressMessage('');
    setSegments([]);
    segmentsRef.current = [];
    setCurrentSegment(0);
    setTotalSegments(null);
    totalSegmentsRef.current = null;
    setContentHash(null);
    contentHashRef.current = null;
    setResumePending(false);
  };

  const handleGenerate = async () => {
    if (!userInstructions.trim()) {
      message.warning(t('instructionsRequired'));
      return;
    }

    setIsGenerating(true);
    setGeneratedText('');
    setProgress(0);
    setProgressMessage(t('preparing'));
    errorNotifiedRef.current = false;

    // 创建 AbortController 用于取消请求
    abortControllerRef.current = new AbortController();

    try {
      await chapterApi.partialRegenerateStream(
        chapterId,
        {
          selected_text: selectedText,
          start_position: startPosition,
          end_position: endPosition,
          user_instructions: userInstructions,
          context_chars: 500,
          style_id: styleId,
          length_mode: lengthMode,
          target_word_count: lengthMode === 'custom' ? customWordCount : undefined,
        },
        {
          onProgress: (msg, prog) => {
            setProgress(prog);
            setProgressMessage(msg);
          },
          onChunk: (content) => {
            setGeneratedText(prev => prev + content);
          },
          onResult: () => {
            setProgress(100);
            setProgressMessage(t('generated'));
            setHasGenerated(true);
          },
          onError: (error) => {
            console.error('SSE错误:', error);
            errorNotifiedRef.current = true;
            message.error(error || t('generateError'));
            setIsGenerating(false);
          },
          onComplete: () => {
            setIsGenerating(false);
            setHasGenerated(true);
          },
        }
      );
    } catch (error) {
      console.error('生成失败:', error);
      if (!errorNotifiedRef.current && (error as Error).name !== 'AbortError') {
        message.error(t('generateFailed'));
      }
      setIsGenerating(false);
    }
  };

  /** 清空续写草稿（分段数组/哈希/段数），重新开始循环前调用 */
  const resetContinueDraft = () => {
    setSegments([]);
    segmentsRef.current = [];
    setCurrentSegment(0);
    setTotalSegments(null);
    totalSegmentsRef.current = null;
    setContentHash(null);
    contentHashRef.current = null;
    setGeneratedText('');
    setResumePending(false);
    setHasGenerated(false);
  };

  /**
   * 客户端分段循环（D9 fork B）：后端为无状态单段原语，循环由本组件编排。
   * startIndex 支持断点续跑（= 已完成段数）。
   */
  const runContinueLoop = async (startIndex: number) => {
    setIsGenerating(true);
    setHasGenerated(false);
    setResumePending(false);
    stopRequestedRef.current = false;
    let k = startIndex;

    try {
      while (true) {
        const baseText = segmentsRef.current.join('');
        errorNotifiedRef.current = false;
        setCurrentSegment(k);
        setProgress(0);
        setProgressMessage(t('preparing'));

        abortControllerRef.current = new AbortController();
        let buffer = '';
        const captured: { result: ContinueSegmentResult | null } = { result: null };

        try {
          await chapterApi.partialRegenerateStream(
            chapterId,
            {
              selected_text: selectedText,
              start_position: startPosition,
              end_position: endPosition,
              user_instructions: userInstructions,
              context_chars: 500,
              style_id: styleId,
              mode: 'continue',
              target_word_count: continueTarget,
              segment_index: k,
              already_generated_chars: baseText.length,
              rolling_context: tail(baseText, ROLLING_CONTEXT_CHARS),
              content_hash: contentHashRef.current ?? undefined,
            },
            {
              signal: abortControllerRef.current.signal,
              onProgress: (msg) => {
                setProgressMessage(msg || t('generating'));
              },
              onChunk: (content) => {
                buffer += content;
                setGeneratedText(baseText + buffer);
                setProgress(Math.min(99, Math.round(((baseText.length + buffer.length) / Math.max(continueTarget, 1)) * 100)));
              },
              onResult: (data) => {
                captured.result = data as ContinueSegmentResult;
              },
              onError: (error) => {
                console.error('SSE错误:', error);
                errorNotifiedRef.current = true;
                message.error(error || t('generateError'));
              },
            }
          );
        } catch (error) {
          // AbortController 只中断当前段：草稿保留，进入断点续写界面
          if (stopRequestedRef.current || (error as Error)?.name === 'AbortError') {
            break;
          }
          throw error;
        }

        // 段完成：以 result 事件的 new_text 固化分段（缺 result 时回退到流式缓冲）
        const newText = captured.result?.new_text ?? buffer;
        if (captured.result?.content_hash && !contentHashRef.current) {
          contentHashRef.current = captured.result.content_hash;
          setContentHash(captured.result.content_hash);
        }
        if (typeof captured.result?.segment_count === 'number' && captured.result.segment_count > 0) {
          totalSegmentsRef.current = captured.result.segment_count;
          setTotalSegments(captured.result.segment_count);
        }
        segmentsRef.current = [...segmentsRef.current, newText];
        setSegments(segmentsRef.current);
        setGeneratedText(segmentsRef.current.join(''));

        const n = totalSegmentsRef.current;
        const finished =
          captured.result?.complete === true ||
          (n !== null && segmentsRef.current.length >= n) ||
          // 无分段元数据时按单段完成处理，避免死循环
          (n === null && captured.result?.complete !== false);
        if (finished) {
          setProgress(100);
          setProgressMessage(t('generated'));
          setHasGenerated(true);
          setIsGenerating(false);
          return;
        }
        if (stopRequestedRef.current) {
          break;
        }
        k += 1;
      }
      // 用户中止当前段：保留草稿（丢弃未完成段的流式缓冲），展示续跑/接受部分结果入口
      setIsGenerating(false);
      setGeneratedText(segmentsRef.current.join(''));
      setResumePending(true);
    } catch (error) {
      console.error('续写生成失败:', error);
      if (!errorNotifiedRef.current && (error as Error).name !== 'AbortError') {
        message.error(t('generateFailed'));
      }
      setIsGenerating(false);
      if (segmentsRef.current.length > 0) {
        // 已有草稿：可从中断处继续或接受已完成部分
        setGeneratedText(segmentsRef.current.join(''));
        setResumePending(true);
      } else {
        setGeneratedText('');
      }
    }
  };

  /** 发起续写：软阈值以上先确认段数估算 */
  const handleStartContinue = () => {
    if (!userInstructions.trim()) {
      message.warning(t('instructionsRequired'));
      return;
    }
    if (continueTarget > SOFT_TARGET_THRESHOLD) {
      modal.confirm({
        title: t('largeTargetTitle'),
        content: t('largeTargetConfirm', {
          target: continueTarget,
          count: Math.ceil(continueTarget / ESTIMATED_SEGMENT_CHARS),
        }),
        okText: t('largeTargetOk'),
        cancelText: t('cancel'),
        onOk: () => {
          resetContinueDraft();
          void runContinueLoop(0);
        },
      });
      return;
    }
    resetContinueDraft();
    void runContinueLoop(0);
  };

  /** 停止当前段生成（草稿保留，出现续跑入口） */
  const handleStopSegment = () => {
    stopRequestedRef.current = true;
    abortControllerRef.current?.abort();
  };

  const handleCancel = () => {
    if (isGenerating && abortControllerRef.current) {
      abortControllerRef.current.abort();
      setIsGenerating(false);
      message.info(t('cancelled'));
    }
    onClose();
  };

  const handleAccept = async () => {
    if (mode === 'continue') {
      const text = segmentsRef.current.join('');
      if (!text.trim()) {
        message.warning(t('noContent'));
        return;
      }
      try {
        // 续写为插入式应用：锚点选区仅定位，start == end，不替换后续文本；整篇只应用一次
        await chapterApi.applyPartialRegenerate(chapterId, {
          new_text: text,
          start_position: endPosition,
          end_position: endPosition,
          mode: 'continue',
          content_hash: contentHash ?? undefined,
        });
        message.success(t('continueApplied'));
        onApply(text, endPosition, endPosition, 'continue');
        onClose();
      } catch (error) {
        console.error('应用失败:', error);
        message.error(t('applyFailed'));
      }
      return;
    }

    if (!generatedText.trim()) {
      message.warning(t('noContent'));
      return;
    }

    try {
      // 调用后端应用更改
      await chapterApi.applyPartialRegenerate(chapterId, {
        new_text: generatedText,
        start_position: startPosition,
        end_position: endPosition,
      });

      message.success(t('applied'));
      onApply(generatedText, startPosition, endPosition, 'rewrite');
      onClose();
    } catch (error) {
      console.error('应用失败:', error);
      message.error(t('applyFailed'));
    }
  };

  const handleRegenerate = () => {
    if (mode === 'continue') {
      void handleStartContinue();
      return;
    }
    setGeneratedText('');
    setHasGenerated(false);
    setProgress(0);
    setProgressMessage('');
    handleGenerate();
  };

  const getLengthModeDescription = (m: LengthMode): string => {
    const descriptions: Record<LengthMode, string> = {
      similar: t('lengthSimilar'),
      expand: t('lengthExpand'),
      condense: t('lengthCondense'),
      custom: t('lengthCustom'),
    };
    return descriptions[m];
  };

  const isContinue = mode === 'continue';

  return (
    <Modal
      title={
        <Space>
          <EditOutlined style={{ color: token.colorPrimary }} />
          <span>{t(isContinue ? 'titleContinue' : 'title')}</span>
        </Space>
      }
      open={visible}
      onCancel={handleCancel}
      width={800}
      centered
      maskClosable={!isGenerating}
      closable={!isGenerating}
      keyboard={!isGenerating}
      footer={
        <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
          <Button onClick={handleCancel} disabled={isGenerating}>
            {t('cancel')}
          </Button>
          {isGenerating && isContinue && (
            <Button icon={<PauseCircleOutlined />} onClick={handleStopSegment}>
              {t('stopGenerate')}
            </Button>
          )}
          {isGenerating && !isContinue && (
            <Button
              type="primary"
              icon={<LoadingOutlined />}
              loading
              disabled={!userInstructions.trim()}
            >
              {t('generating')}
            </Button>
          )}
          {!isGenerating && !hasGenerated && (!resumePending || segments.length === 0) && (
            isContinue ? (
              <Button
                type="primary"
                icon={<ThunderboltOutlined />}
                onClick={handleStartContinue}
                disabled={!userInstructions.trim()}
                style={{
                  background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`,
                  border: 'none',
                  boxShadow: token.boxShadowSecondary,
                }}
              >
                {t('startContinue')}
              </Button>
            ) : (
              <Button
                type="primary"
                icon={<ThunderboltOutlined />}
                onClick={handleGenerate}
                disabled={!userInstructions.trim()}
                style={{
                  background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`,
                  border: 'none',
                  boxShadow: token.boxShadowSecondary,
                }}
              >
                {t('startRewrite')}
              </Button>
            )
          )}
          {(hasGenerated || (resumePending && !isGenerating && segments.length > 0)) && (
            <>
              {hasGenerated && (
                <Button icon={<ReloadOutlined />} onClick={handleRegenerate}>
                  {t('regenerate')}
                </Button>
              )}
              {resumePending && !isGenerating && (
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => void runContinueLoop(segmentsRef.current.length)}
                >
                  {t('resumeFromSegment', { segment: segments.length + 1 })}
                </Button>
              )}
              <Button
                type="primary"
                icon={<CheckOutlined />}
                onClick={handleAccept}
                style={{ background: token.colorSuccess, borderColor: token.colorSuccess }}
              >
                {isContinue ? t(resumePending ? 'acceptPartial' : 'applyContinue') : t('apply')}
              </Button>
            </>
          )}
        </Space>
      }
      styles={{
        body: {
          maxHeight: 'calc(100vh - 200px)',
          overflowY: 'auto',
        },
      }}
    >
      {/* 模式切换：改写 / 续写 */}
      <div style={{ marginBottom: 16 }}>
        <Segmented
          value={mode}
          onChange={(value) => handleModeChange(value as RegenerateMode)}
          disabled={isGenerating}
          options={[
            { label: t('modeRewrite'), value: 'rewrite' },
            { label: t('modeContinue'), value: 'continue' },
          ]}
        />
      </div>

      {/* 原文展示 */}
      <Card
        size="small"
        title={
          <Space>
            <Text strong>{t('originalText')}</Text>
            <Text type="secondary">({t('charCount', { count: selectedText.length })})</Text>
          </Space>
        }
        style={{ marginBottom: 16 }}
        styles={{
          body: {
            maxHeight: 150,
            overflowY: 'auto',
            background: token.colorFillAlter,
          },
        }}
      >
        <Paragraph
          style={{
            margin: 0,
            whiteSpace: 'pre-wrap',
            color: token.colorText,
            lineHeight: 1.8,
          }}
        >
          {selectedText}
        </Paragraph>
      </Card>

      {/* 改写/续写要求输入 */}
      <div style={{ marginBottom: 16 }}>
        <Text strong style={{ display: 'block', marginBottom: 8 }}>
          {t(isContinue ? 'continueRequirementsLabel' : 'rewriteRequirementsLabel')} <Text type="danger">*</Text>
        </Text>
        <TextArea
          value={userInstructions}
          onChange={(e) => setUserInstructions(e.target.value)}
          placeholder={t(isContinue ? 'continuePlaceholder' : 'placeholder')}
          rows={4}
          disabled={isGenerating}
          style={{ resize: 'none' }}
        />
      </div>

      {isContinue ? (
        /* 续写长度控制：绝对目标字数（无硬上限） */
        <div style={{ marginBottom: 16 }}>
          <Text strong style={{ display: 'block', marginBottom: 8 }}>
            {t('continueLengthLabel')}
          </Text>
          <Space>
            <InputNumber
              value={continueTarget}
              onChange={(value) => setContinueTarget(typeof value === 'number' && value > 0 ? value : 1000)}
              min={1}
              step={500}
              disabled={isGenerating}
              addonAfter={t('wordUnit')}
              style={{ width: 180 }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t('continueLengthHint')}
            </Text>
          </Space>
          {continueTarget > SOFT_TARGET_THRESHOLD && (
            <div style={{ marginTop: 8 }}>
              <Text type="warning" style={{ fontSize: 12 }}>
                {t('largeTargetHint', { count: Math.ceil(continueTarget / ESTIMATED_SEGMENT_CHARS) })}
              </Text>
            </div>
          )}
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 12 }}
            message={t('continueAnchorTitle')}
            description={
              <div>
                <div style={{ whiteSpace: 'pre-line' }}>{t('continueExplain')}</div>
                <Text type="warning" style={{ fontSize: 12 }}>{t('continueForkNote')}</Text>
              </div>
            }
          />
        </div>
      ) : (
        /* 长度模式选择（改写） */
        <div style={{ marginBottom: 16 }}>
          <Text strong style={{ display: 'block', marginBottom: 8 }}>
            {t('lengthControlLabel')}
          </Text>
          <Radio.Group
            value={lengthMode}
            onChange={(e) => setLengthMode(e.target.value)}
            disabled={isGenerating}
            buttonStyle="solid"
          >
            <Radio.Button value="similar">{t('lengthKeepLabel')}</Radio.Button>
            <Radio.Button value="expand">{t('lengthExpandLabel')}</Radio.Button>
            <Radio.Button value="condense">{t('lengthCondenseLabel')}</Radio.Button>
            <Radio.Button value="custom">{t('lengthCustomLabel')}</Radio.Button>
          </Radio.Group>
          <div style={{ marginTop: 8 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {getLengthModeDescription(lengthMode)}
            </Text>
          </div>
          {lengthMode === 'custom' && (
            <div style={{ marginTop: 12 }}>
              <Space>
                <Text>{t('targetWordCountLabel')}</Text>
                <InputNumber
                  value={customWordCount}
                  onChange={(value) => setCustomWordCount(value || selectedText.length)}
                  min={10}
                  max={10000}
                  step={50}
                  disabled={isGenerating}
                  addonAfter={t('wordUnit')}
                  style={{ width: 150 }}
                />
              </Space>
            </div>
          )}
        </div>
      )}

      <Divider style={{ margin: '16px 0' }} />

      {/* 生成结果展示 */}
      {(isGenerating || hasGenerated || (resumePending && segments.length > 0)) && (
        <div>
          <div style={{ 
            display: 'flex', 
            justifyContent: 'space-between', 
            alignItems: 'center',
            marginBottom: 8 
          }}>
            <Space>
              <Text strong>{t(isContinue ? 'continueResultLabel' : 'resultLabel')}</Text>
              {generatedText && (
                <Text type="secondary">({t('charCount', { count: generatedText.length })})</Text>
              )}
              {isContinue && isGenerating && (
                <Text type="secondary">
                  {totalSegments !== null
                    ? t('segmentProgress', { current: currentSegment + 1, total: totalSegments })
                    : t('segmentProgressUnknown', { current: currentSegment + 1 })}
                </Text>
              )}
            </Space>
            {isGenerating && (
              <Space>
                <Spin indicator={<LoadingOutlined style={{ fontSize: 14 }} spin />} />
                <Text type="secondary">{progressMessage || t('generating')}</Text>
              </Space>
            )}
          </div>

          {/* 进度条 */}
          {isGenerating && (
            <div style={{ marginBottom: 12 }}>
              <div
                style={{
                  height: 4,
                  background: token.colorFillTertiary,
                  borderRadius: 2,
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    height: '100%',
                    background: `linear-gradient(90deg, ${token.colorPrimary} 0%, ${token.colorPrimaryHover} 100%)`,
                    width: `${progress}%`,
                    transition: 'width 0.3s ease',
                    borderRadius: 2,
                  }}
                />
              </div>
            </div>
          )}

          <Card
            size="small"
            ref={generatedTextRef}
            style={{
              background: generatedText ? token.colorSuccessBg : token.colorFillAlter,
              border: generatedText ? `1px solid ${token.colorSuccessBorder}` : `1px solid ${token.colorBorder}`,
            }}
            styles={{
              body: {
                maxHeight: 250,
                overflowY: 'auto',
                minHeight: 100,
              },
            }}
          >
            {generatedText ? (
              <Paragraph
                style={{
                  margin: 0,
                  whiteSpace: 'pre-wrap',
                  lineHeight: 1.8,
                }}
              >
                {generatedText}
                {isGenerating && (
                  <span
                    style={{
                      display: 'inline-block',
                      width: 8,
                      height: 16,
                      background: token.colorPrimary,
                      marginLeft: 2,
                      animation: 'blink 1s infinite',
                    }}
                  />
                )}
              </Paragraph>
            ) : (
              <div style={{ textAlign: 'center', padding: 20, color: token.colorTextTertiary }}>
                {isGenerating ? t('generatingContent') : t('waitingGenerate')}
              </div>
            )}
          </Card>

          {isContinue && resumePending && !isGenerating && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 12 }}
              message={t('resumeTitle')}
              description={t('resumeDesc', { count: segments.length })}
            />
          )}

          {hasGenerated && generatedText && (
            <Alert
              message={t('generated')}
              description={
                isContinue ? (
                  <span>{t('continueSummary', { count: generatedText.length })}</span>
                ) : (
                  <span>
                    {t('resultSummary', { original: selectedText.length, generated: generatedText.length })}
                    {generatedText.length > selectedText.length && (
                      <Text type="success">{t('diffPositive', { delta: generatedText.length - selectedText.length })}</Text>
                    )}
                    {generatedText.length < selectedText.length && (
                      <Text type="warning">{t('diffNegative', { delta: generatedText.length - selectedText.length })}</Text>
                    )}
                  </span>
                )
              }
              type="success"
              showIcon
              style={{ marginTop: 12 }}
            />
          )}
        </div>
      )}

      {/* 添加闪烁光标动画 */}
      <style>{`
        @keyframes blink {
          0%, 50% { opacity: 1; }
          51%, 100% { opacity: 0; }
        }
      `}</style>
    </Modal>
  );
};

export default PartialRegenerateModal;
