import { App, Modal, Form, Input, InputNumber, Select, Tag, Space, Button, Divider } from 'antd';
import { useTranslation } from 'react-i18next';
import { PlusOutlined } from '@ant-design/icons';
import { useState, useEffect, useCallback } from 'react';
import type { ExpansionPlanData, Character } from '../types';
import { characterApi } from '../services/api';

const { TextArea } = Input;

interface ExpansionPlanEditorProps {
  visible: boolean;
  planData: ExpansionPlanData | null;
  chapterSummary: string | null;
  projectId: string;
  onSave: (data: ExpansionPlanData & { summary?: string }) => Promise<void>;
  onCancel: () => void;
}

export default function ExpansionPlanEditor({
  visible,
  planData,
  chapterSummary,
  projectId,
  onSave,
  onCancel
}: ExpansionPlanEditorProps) {
  const { message } = App.useApp();
  const { t } = useTranslation('expansionPlanEditor');
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  
  // 关键事件标签输入
  const [keyEventInput, setKeyEventInput] = useState('');
  const [keyEvents, setKeyEvents] = useState<string[]>([]);
  
  // 角色列表和选择
  const [availableCharacters, setAvailableCharacters] = useState<Character[]>([]);
  const [characters, setCharacters] = useState<string[]>([]);
  const [loadingCharacters, setLoadingCharacters] = useState(false);

  // 加载项目角色列表
  const loadCharacters = useCallback(async () => {
    try {
      setLoadingCharacters(true);
      setAvailableCharacters([]); // 重置为空数组
      const response = await characterApi.getCharacters(projectId);
      console.log('加载到的角色数据:', response);
      
      // API返回的是 {total, items} 格式,需要提取items
      let chars: Character[] = [];
      if (Array.isArray(response)) {
        chars = response;
      } else if (response && typeof response === 'object' && 'items' in response) {
        const responseObj = response as { items?: Character[] };
        if (Array.isArray(responseObj.items)) {
          chars = responseObj.items;
        }
      } else {
        console.error('角色API返回格式异常:', response);
        message.warning(t('dataFormatError'));
      }
      
      setAvailableCharacters(chars);
      console.log('设置的角色列表:', chars);
    } catch (error: unknown) {
      console.error('加载角色列表失败:', error);
      setAvailableCharacters([]);
      const err = error as Error;
      message.error(t('loadCharactersFailed', { error: err?.message || t('unknownError') }));
    } finally {
      setLoadingCharacters(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (visible && projectId) {
      loadCharacters();
    }
  }, [visible, projectId, loadCharacters]);

  // 当planData或chapterSummary变化时更新状态
  useEffect(() => {
    if (visible) {
      if (planData) {
        setKeyEvents(planData.key_events || []);
        setCharacters(planData.character_focus || []);
        form.setFieldsValue({
          summary: chapterSummary || '',
          emotional_tone: planData.emotional_tone,
          narrative_goal: planData.narrative_goal,
          conflict_type: planData.conflict_type,
          estimated_words: planData.estimated_words
        });
      } else {
        // 重置状态
        setKeyEvents([]);
        setCharacters([]);
        form.setFieldsValue({
          summary: chapterSummary || ''
        });
      }
    }
  }, [planData, chapterSummary, form, visible]);

  const handleAddKeyEvent = () => {
    if (keyEventInput.trim()) {
      setKeyEvents([...keyEvents, keyEventInput.trim()]);
      setKeyEventInput('');
    }
  };

  const handleAddCharacter = (characterName: string) => {
    if (characterName && !characters.includes(characterName)) {
      setCharacters([...characters, characterName]);
    }
  };

  const handleSubmit = async () => {
    try {
      setLoading(true);
      const values = await form.validateFields();
      
      // 验证至少有一个关键事件
      if (keyEvents.length === 0) {
        message.warning(t('keyEventRequired'));
        setLoading(false);
        return;
      }
      
      // 验证至少有一个角色
      if (characters.length === 0) {
        message.warning(t('characterRequired'));
        setLoading(false);
        return;
      }
      
      const updatedPlan: ExpansionPlanData & { summary?: string } = {
        summary: values.summary,
        key_events: keyEvents,
        character_focus: characters,
        emotional_tone: values.emotional_tone,
        narrative_goal: values.narrative_goal,
        conflict_type: values.conflict_type,
        estimated_words: values.estimated_words,
        scenes: planData?.scenes || null
      };
      
      await onSave(updatedPlan);
      // message.success('规划信息保存成功');
    } catch (error) {
      console.error('保存失败:', error);
      message.error(t('saveFailed'));
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = () => {
    form.resetFields();
    setKeyEvents([]);
    setCharacters([]);
    setKeyEventInput('');
    onCancel();
  };

  return (
    <Modal
      title={t('title')}
      open={visible}
      onCancel={handleCancel}
      width={700}
      centered
      footer={[
        <Button key="cancel" onClick={handleCancel} disabled={loading}>
          {t('cancel')}
        </Button>,
        <Button key="submit" type="primary" loading={loading} onClick={handleSubmit}>
          {t('save')}
        </Button>
      ]}
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          emotional_tone: t('defaultEmotionalTone'),
          conflict_type: t('defaultConflictType'),
          estimated_words: 3000
        }}
      >
        {/* 情节概要 */}
        <Form.Item
          label={t('summaryLabel')}
          name="summary"
          tooltip={t('summaryTooltip')}
        >
          <TextArea
            rows={3}
            placeholder={t('summaryPlaceholder')}
            maxLength={500}
            showCount
          />
        </Form.Item>

        <Divider orientation="left">{t('detailedPlan')}</Divider>

        {/* 关键事件 */}
        <Form.Item
          label={t('keyEventsLabel')}
          tooltip={t('keyEventsTooltip')}
          required
        >
          <Space direction="vertical" style={{ width: '100%' }}>
            <Space.Compact style={{ width: '100%' }}>
              <Input
                placeholder={t('keyEventPlaceholder')}
                value={keyEventInput}
                onChange={(e) => setKeyEventInput(e.target.value)}
                onPressEnter={handleAddKeyEvent}
              />
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={handleAddKeyEvent}
              >
                {t('add')}
              </Button>
            </Space.Compact>
            <Space wrap>
              {keyEvents.map((event, idx) => (
                <Tag
                  key={idx}
                  closable
                  onClose={(e) => {
                    e.preventDefault();
                    setKeyEvents(keyEvents.filter((_, i) => i !== idx));
                  }}
                  color="purple"
                  style={{ marginBottom: 8 }}
                >
                  <span style={{ fontWeight: 'bold', marginRight: 4 }}>#{idx + 1}</span>
                  {event}
                </Tag>
              ))}
            </Space>
          </Space>
        </Form.Item>

        {/* 涉及角色 */}
        <Form.Item
          label={t('charactersLabel')}
          tooltip={t('charactersTooltip')}
          required
        >
          <Space direction="vertical" style={{ width: '100%' }}>
            <Select
              placeholder={t('selectCharacterPlaceholder')}
              style={{ width: '100%' }}
              loading={loadingCharacters}
              onChange={handleAddCharacter}
              value={undefined}
              showSearch
              optionFilterProp="children"
              filterOption={(input, option) =>
                (option?.label ?? '').toLowerCase().includes(input.toLowerCase())
              }
              options={Array.isArray(availableCharacters)
                ? availableCharacters
                    .filter(char => !characters.includes(char.name))
                    .map(char => ({
                      label: char.name,
                      value: char.name,
                    }))
                : []}
              notFoundContent={
                loadingCharacters ? t('loading') :
                !Array.isArray(availableCharacters) ? t('loadCharactersError') :
                availableCharacters.length === 0 ? t('noCharacters') :
                t('allCharactersAdded')
              }
            />
            <Space wrap>
              {characters.map((char, idx) => (
                <Tag
                  key={idx}
                  closable
                  onClose={() => setCharacters(characters.filter((_, i) => i !== idx))}
                  color="cyan"
                >
                  {char}
                </Tag>
              ))}
            </Space>
          </Space>
        </Form.Item>

        {/* 情感基调 */}
        <Form.Item
          label={t('emotionalToneLabel')}
          name="emotional_tone"
          rules={[{ required: true, message: t('emotionalToneRequired') }]}
          tooltip={t('emotionalToneTooltip')}
        >
          <Input
            placeholder={t('emotionalTonePlaceholder')}
            maxLength={20}
          />
        </Form.Item>

        {/* 冲突类型 */}
        <Form.Item
          label={t('conflictTypeLabel')}
          name="conflict_type"
          rules={[{ required: true, message: t('conflictTypeRequired') }]}
          tooltip={t('conflictTypeTooltip')}
        >
          <Input
            placeholder={t('conflictTypePlaceholder')}
            maxLength={20}
          />
        </Form.Item>

        {/* 预估字数 */}
        <Form.Item
          label={t('estimatedWordsLabel')}
          name="estimated_words"
          rules={[{ required: true, message: t('estimatedWordsRequired') }]}
        >
          <InputNumber
            min={500}
            max={10000}
            step={100}
            style={{ width: '100%' }}
            formatter={(value) => t('wordCountFormat', { value: value ?? 0 })}
            parser={(value) => Number((value || '').replace(/[^\d]/g, '')) as 500 | 10000}
          />
        </Form.Item>

        {/* 叙事目标 */}
        <Form.Item
          label={t('narrativeGoalLabel')}
          name="narrative_goal"
          rules={[{ required: true, message: t('narrativeGoalRequired') }]}
        >
          <TextArea
            rows={3}
            placeholder={t('narrativeGoalPlaceholder')}
            maxLength={500}
            showCount
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}