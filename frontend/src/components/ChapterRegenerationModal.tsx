import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Modal,
  Form,
  Input,
  Button,
  Checkbox,
  InputNumber,
  Space,
  Alert,
  Divider,
  Tag,
  Collapse,
  Card,
  Radio,
  App
} from 'antd';
import {
  ReloadOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined
} from '@ant-design/icons';
import { ssePost } from '../utils/sseClient';
import { SSEProgressModal } from './SSEProgressModal';

const { TextArea } = Input;
const { Panel } = Collapse;

interface Suggestion {
  category: string;
  content: string;
  priority: string;
}

interface ChapterRegenerationModalProps {
  visible: boolean;
  onCancel: () => void;
  onSuccess: (newContent: string, wordCount: number) => void;
  chapterId: string;
  chapterTitle: string;
  chapterNumber: number;
  suggestions?: Suggestion[];
  hasAnalysis: boolean;
}


const ChapterRegenerationModal: React.FC<ChapterRegenerationModalProps> = ({
  visible,
  onCancel,
  onSuccess,
  chapterId,
  chapterTitle,
  chapterNumber,
  suggestions = [],
  hasAnalysis
}) => {
  const { message } = App.useApp();
  const { t } = useTranslation('chapterRegenerationModal');
  const [form] = Form.useForm();
  const [modal, contextHolder] = Modal.useModal();
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [status, setStatus] = useState<'idle' | 'generating' | 'success' | 'error'>('idle');
  const [errorMessage, setErrorMessage] = useState('');
  const [wordCount, setWordCount] = useState(0);
  const [selectedSuggestions, setSelectedSuggestions] = useState<number[]>([]);
  const [modificationSource, setModificationSource] = useState<'custom' | 'analysis_suggestions' | 'mixed'>('custom');

  useEffect(() => {
    if (visible) {
      // 重置状态
      setStatus('idle');
      setProgress(0);
      setErrorMessage('');
      setWordCount(0);
      setSelectedSuggestions([]);
      
      // 如果有分析建议，默认选择混合模式
      if (hasAnalysis && suggestions.length > 0) {
        setModificationSource('mixed');
      } else {
        setModificationSource('custom');
      }
      
      // 设置默认值
      form.setFieldsValue({
        modification_source: hasAnalysis && suggestions.length > 0 ? 'mixed' : 'custom',
        target_word_count: 3000,
        preserve_structure: false,
        preserve_character_traits: true,
        focus_areas: []
      });
    }
  }, [visible, hasAnalysis, suggestions.length, form]);

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      
      // 验证至少提供一种修改指令
      if (values.modification_source === 'custom' && !values.custom_instructions?.trim()) {
        message.error(t('customRequired'));
        return;
      }
      
      if (values.modification_source === 'analysis_suggestions' && selectedSuggestions.length === 0) {
        message.error(t('suggestionRequired'));
        return;
      }
      
      if (values.modification_source === 'mixed' && 
          selectedSuggestions.length === 0 && 
          !values.custom_instructions?.trim()) {
        message.error(t('mixedRequired'));
        return;
      }

      setLoading(true);
      setStatus('generating');
      setProgress(0);
      setWordCount(0);

      // 构建请求数据
      interface RegenerationRequest {
        modification_source: string;
        custom_instructions?: string;
        selected_suggestion_indices: number[];
        preserve_elements: {
          preserve_structure: boolean;
          preserve_dialogues: string[];
          preserve_plot_points: string[];
          preserve_character_traits: boolean;
        };
        style_id?: string;
        target_word_count: number;
        focus_areas: string[];
      }

      const requestData: RegenerationRequest = {
        modification_source: values.modification_source,
        custom_instructions: values.custom_instructions,
        selected_suggestion_indices: selectedSuggestions,
        preserve_elements: {
          preserve_structure: values.preserve_structure,
          preserve_dialogues: values.preserve_dialogues || [],
          preserve_plot_points: values.preserve_plot_points || [],
          preserve_character_traits: values.preserve_character_traits
        },
        style_id: values.style_id,
        target_word_count: values.target_word_count,
        focus_areas: values.focus_areas || []
      };

      let accumulatedContent = '';
      let currentWordCount = 0;

      // 使用SSE流式生成
      await ssePost(
        `/api/chapters/${chapterId}/regenerate-stream`,
        requestData,
        {
          onProgress: (_msg: string, prog: number, _status: string, wordCount?: number) => {
            // 后端发送的进度消息
            setProgress(prog);
            // 如果后端提供了word_count，使用它；否则使用累积的字数
            if (wordCount !== undefined) {
              setWordCount(wordCount);
              currentWordCount = wordCount;
            }
          },
          onChunk: (content: string) => {
            // 累积内容块
            accumulatedContent += content;
            // 仅作为备用字数统计
            currentWordCount = accumulatedContent.length;
            // 不再自己计算进度，完全依赖后端发送的progress消息
          },
          onResult: (data: { word_count?: number }) => {
            // 生成完成，确保使用最新的累积内容
            setProgress(100);
            setStatus('success');
            const finalWordCount = data.word_count || currentWordCount;
            setWordCount(finalWordCount);
            message.success(t('regenerateSuccess'));
            
            // 直接调用onSuccess打开对比界面，传递最终的累积内容
            setTimeout(() => {
              onSuccess(accumulatedContent, finalWordCount);
            }, 500);
          },
          onComplete: () => {
            // SSE完成
          },
          onError: (error: string, code?: number) => {
            console.error('SSE Error:', error, code);
            setStatus('error');
            setErrorMessage(error || t('generateFailed'));
            message.error(t('regenerateFailed', { error: error || t('unknownError') }));
          }
        }
      );

    } catch (error: unknown) {
      console.error('提交失败:', error);
      setStatus('error');
      const err = error as Error;
      setErrorMessage(err.message || t('submitFailed'));
      message.error(t('operationFailed', { error: err.message || t('unknownError') }));
    } finally {
      setLoading(false);
    }
  };

  const handleSuggestionSelect = (index: number, checked: boolean) => {
    if (checked) {
      setSelectedSuggestions([...selectedSuggestions, index]);
    } else {
      setSelectedSuggestions(selectedSuggestions.filter(i => i !== index));
    }
  };

  const handleCancel = () => {
    if (loading) {
      modal.confirm({
        title: t('cancelConfirmTitle'),
        content: t('cancelConfirmContent'),
        centered: true,
        onOk: () => {
          setLoading(false);
          setStatus('idle');
          onCancel();
        }
      });
    } else {
      onCancel();
    }
  };

  return (
    <>
      {contextHolder}
      <Modal
      title={t('title', { number: chapterNumber, title: chapterTitle })}
      open={visible}
      onCancel={handleCancel}
      width={800}
      centered
      footer={
        status === 'success' ? null : (
          [
            <Button key="cancel" onClick={handleCancel} disabled={loading}>
              {t('cancel')}
            </Button>,
            <Button
              key="submit"
              type="primary"
              onClick={handleSubmit}
              loading={loading}
              icon={<ReloadOutlined />}
            >
              {t('startRegenerate')}
            </Button>
          ]
        )
      }
    >

      {status === 'success' && (
        <Alert
          message={t('regenerateSuccess')}
          description={t('generatedSummary', { count: wordCount })}
          type="success"
          showIcon
          icon={<CheckCircleOutlined />}
          style={{ marginBottom: 16 }}
        />
      )}

      {status === 'error' && (
        <Alert
          message={t('generateFailed')}
          description={errorMessage}
          type="error"
          showIcon
          icon={<CloseCircleOutlined />}
          style={{ marginBottom: 16 }}
        />
      )}

      <Form
        form={form}
        layout="vertical"
        disabled={loading || status === 'success'}
      >
        {/* 修改来源 */}
        <Form.Item
          name="modification_source"
          label={t('modificationSourceLabel')}
          rules={[{ required: true, message: t('modificationSourceRequired') }]}
        >
          <Radio.Group onChange={(e) => setModificationSource(e.target.value)}>
            <Radio value="custom">{t('customOnly')}</Radio>
            {hasAnalysis && suggestions.length > 0 && (
              <>
                <Radio value="analysis_suggestions">{t('analysisOnly')}</Radio>
                <Radio value="mixed">{t('mixedMode')}</Radio>
              </>
            )}
          </Radio.Group>
        </Form.Item>

        {/* 分析建议选择 */}
        {hasAnalysis && suggestions.length > 0 && 
         (modificationSource === 'analysis_suggestions' || modificationSource === 'mixed') && (
          <Form.Item label={t('suggestionsLabel', { selected: selectedSuggestions.length, total: suggestions.length })}>
            <Card size="small" style={{ maxHeight: 300, overflow: 'auto' }}>
              <Space direction="vertical" style={{ width: '100%' }}>
                {suggestions.map((suggestion, index) => (
                  <Checkbox
                    key={index}
                    checked={selectedSuggestions.includes(index)}
                    onChange={(e) => handleSuggestionSelect(index, e.target.checked)}
                  >
                    <Space>
                      <Tag color={
                        suggestion.priority === 'high' ? 'red' :
                        suggestion.priority === 'medium' ? 'orange' : 'blue'
                      }>
                        {suggestion.category}
                      </Tag>
                      <span style={{ fontSize: 13 }}>{suggestion.content}</span>
                    </Space>
                  </Checkbox>
                ))}
              </Space>
            </Card>
          </Form.Item>
        )}

        {/* 自定义修改要求 */}
        {(modificationSource === 'custom' || modificationSource === 'mixed') && (
          <Form.Item
            name="custom_instructions"
            label={t('customInstructionsLabel')}
            tooltip={t('customInstructionsTooltip')}
          >
            <TextArea
              rows={4}
              placeholder={t('customInstructionsPlaceholder')}
              showCount
              maxLength={1000}
            />
          </Form.Item>
        )}

        {/* 高级选项 */}
        <Collapse ghost>
          <Panel header={t('advancedOptions')} key="advanced">
            {/* 重点优化方向 */}
            <Form.Item
              name="focus_areas"
              label={t('focusAreasLabel')}
            >
              <Checkbox.Group>
                <Space direction="vertical">
                  <Checkbox value="pacing">{t('focusPacing')}</Checkbox>
                  <Checkbox value="emotion">{t('focusEmotion')}</Checkbox>
                  <Checkbox value="description">{t('focusDescription')}</Checkbox>
                  <Checkbox value="dialogue">{t('focusDialogue')}</Checkbox>
                  <Checkbox value="conflict">{t('focusConflict')}</Checkbox>
                </Space>
              </Checkbox.Group>
            </Form.Item>

            <Divider />

            {/* 保留元素 */}
            <Form.Item label={t('preserveLabel')}>
              <Space direction="vertical" style={{ width: '100%' }}>
                <Form.Item name="preserve_structure" valuePropName="checked" noStyle>
                  <Checkbox>{t('preserveStructure')}</Checkbox>
                </Form.Item>
                <Form.Item name="preserve_character_traits" valuePropName="checked" noStyle>
                  <Checkbox>{t('preserveTraits')}</Checkbox>
                </Form.Item>
              </Space>
            </Form.Item>

            <Divider />

            {/* 生成参数 */}
            <Form.Item
              name="target_word_count"
              label={t('targetWordCountLabel')}
              tooltip={t('targetWordCountTooltip')}
            >
              <InputNumber min={500} max={10000} step={500} style={{ width: '100%' }} />
            </Form.Item>

          </Panel>
        </Collapse>
      </Form>

      {/* 使用统一的进度显示组件 */}
      <SSEProgressModal
        visible={status === 'generating'}
        progress={progress}
        message={t('regeneratingProgress', { count: wordCount })}
        title={t('regenerateTitle')}
      />
      </Modal>
    </>
  );
};

export default ChapterRegenerationModal;