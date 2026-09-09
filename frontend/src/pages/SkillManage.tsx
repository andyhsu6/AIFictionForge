import { useState, useEffect } from 'react';
import { Button, Table, Modal, Form, Input, Tag, Space, message, Popconfirm, Card, theme, Empty, Badge, Tooltip, Select } from 'antd';
import { PlusOutlined, EditOutlined, DeleteOutlined, ReloadOutlined, ThunderboltOutlined, FileTextOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

const { TextArea } = Input;

interface SkillItem {
  template_key: string;
  name: string;
  template_name: string;
  display_name: string;
  category: string;
  description: string;
  triggers: string[];
}

interface SkillDetail {
  template_key: string;
  name: string;
  template_name: string;
  display_name: string;
  category: string;
  description: string;
  triggers: string[];
  body: string;
  raw_content: string;
  standalone_references: Record<string, string>;
}

const SKILL_CATEGORY_OPTIONS = [
  { label: 'Skill·长篇', value: 'Skill·长篇' },
  { label: 'Skill·短篇', value: 'Skill·短篇' },
  { label: 'Skill·润色', value: 'Skill·润色' },
  { label: 'Skill·工具', value: 'Skill·工具' },
  { label: 'Skill', value: 'Skill' },
];

const parseTriggers = (value: string): string[] => (
  (value || '')
    .split(/[\n,，、]+/)
    .map(item => item.trim())
    .filter(Boolean)
    .filter((item, index, array) => array.indexOf(item) === index)
);

const formatTriggers = (triggers: string[]) => (triggers || []).join('\n');

const normalizeCategory = (value: string | string[]) => (
  Array.isArray(value) ? (value[0] || '').trim() : (value || '').trim()
);

export default function SkillManage() {
  const { t } = useTranslation('skillManage');
  const { token } = theme.useToken();
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [createModalVisible, setCreateModalVisible] = useState(false);
  const [editingSkill, setEditingSkill] = useState<SkillDetail | null>(null);
  const [editForm] = Form.useForm();
  const [createForm] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [viewModalVisible, setViewModalVisible] = useState(false);
  const [viewingContent, setViewingContent] = useState('');

  // 加载 Skill 列表
  const loadSkills = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/skills/list');
      if (response.ok) {
        const data = await response.json();
        setSkills(data);
      }
    } catch {
      message.error(t('toast.loadListFailed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadSkills();
  }, []);

  // 打开编辑弹窗
  const handleEdit = async (skill: SkillItem) => {
    try {
      const response = await fetch(`/api/skills/detail/${skill.template_key}`);
      if (response.ok) {
        const detail: SkillDetail = await response.json();
        setEditingSkill(detail);
        editForm.setFieldsValue({
          name: detail.name,
          display_name: detail.display_name || detail.template_name,
          category: detail.category,
          description: detail.description,
          triggers: formatTriggers(detail.triggers),
          body: detail.body,
          references: JSON.stringify(detail.standalone_references, null, 2),
        });
        setEditModalVisible(true);
      } else {
        message.error(t('toast.getDetailFailed'));
      }
    } catch {
      message.error(t('toast.getDetailFailed'));
    }
  };

  // 打开查看原始内容弹窗
  const handleViewRaw = async (skill: SkillItem) => {
    try {
      const response = await fetch(`/api/skills/detail/${skill.template_key}`);
      if (response.ok) {
        const detail: SkillDetail = await response.json();
        setViewingContent(detail.raw_content);
        setViewModalVisible(true);
      }
    } catch {
      message.error(t('toast.getContentFailed'));
    }
  };

  // 保存编辑
  const handleSaveEdit = async () => {
    if (!editingSkill) return;
    const values = await editForm.validateFields();
    setSaving(true);
    try {
      // 解析 references JSON
      let refs: Record<string, string> | undefined;
      if (values.references?.trim()) {
        try {
          refs = JSON.parse(values.references);
        } catch {
          message.error(t('toast.refsJsonError'));
          setSaving(false);
          return;
        }
      }

      const triggers = parseTriggers(values.triggers);
      if (triggers.length === 0) {
        message.error(t('toast.triggerRequired'));
        setSaving(false);
        return;
      }
      const category = normalizeCategory(values.category);
      if (!category) {
        message.error(t('toast.categoryRequired'));
        setSaving(false);
        return;
      }

      const response = await fetch(`/api/skills/update/${editingSkill.template_key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          display_name: values.display_name,
          category,
          description: values.description,
          triggers,
          body: values.body,
          references: refs,
        }),
      });

      if (response.ok) {
        message.success(t('toast.updateSuccess'));
        setEditModalVisible(false);
        loadSkills();
      } else {
        const err = await response.json();
        message.error(err.detail || t('toast.updateFailed'));
      }
    } catch {
      message.error(t('toast.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  // 创建新 Skill
  const handleCreate = async () => {
    const values = await createForm.validateFields();
    setSaving(true);
    try {
      let refs: Record<string, string> | undefined;
      if (values.references?.trim()) {
        try {
          refs = JSON.parse(values.references);
        } catch {
          message.error(t('toast.refsJsonError'));
          setSaving(false);
          return;
        }
      }

      const triggers = parseTriggers(values.triggers);
      if (triggers.length === 0) {
        message.error(t('toast.triggerRequired'));
        setSaving(false);
        return;
      }
      const category = normalizeCategory(values.category);
      if (!category) {
        message.error(t('toast.categoryRequired'));
        setSaving(false);
        return;
      }

      const response = await fetch('/api/skills/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: values.name,
          display_name: values.display_name,
          category,
          description: values.description,
          triggers,
          body: values.body,
          references: refs,
        }),
      });

      if (response.ok) {
        message.success(t('toast.createSuccess'));
        setCreateModalVisible(false);
        createForm.resetFields();
        loadSkills();
      } else {
        const err = await response.json();
        message.error(err.detail || t('toast.createFailed'));
      }
    } catch {
      message.error(t('toast.createFailed'));
    } finally {
      setSaving(false);
    }
  };

  // 删除 Skill
  const handleDelete = async (skillKey: string) => {
    try {
      const response = await fetch(`/api/skills/delete/${skillKey}`, { method: 'DELETE' });
      if (response.ok) {
        message.success(t('toast.deleteSuccess'));
        loadSkills();
      } else {
        const err = await response.json();
        message.error(err.detail || t('toast.deleteFailed'));
      }
    } catch {
      message.error(t('toast.deleteFailed'));
    }
  };

  const columns = [
    {
      title: t('table.name'),
      dataIndex: 'display_name',
      key: 'display_name',
      width: 220,
      ellipsis: true,
      render: (text: string, record: SkillItem) => (
        <div style={{ minWidth: 0 }}>
          <Tooltip title={text}>
            <strong style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{text}</strong>
          </Tooltip>
          <Tooltip title={record.name || record.template_key}>
            <span style={{ display: 'block', marginTop: 2, color: token.colorTextTertiary, fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {record.name || record.template_key}
            </span>
          </Tooltip>
        </div>
      ),
    },
    {
      title: t('table.category'),
      dataIndex: 'category',
      key: 'category',
      width: 120,
      render: (cat: string) => {
        const colorMap: Record<string, string> = {
          'Skill·长篇': 'blue',
          'Skill·短篇': 'green',
          'Skill·润色': 'orange',
          'Skill·工具': 'purple',
          'Skill': 'default',
        };
        return <Tag color={colorMap[cat] || 'default'}>{cat}</Tag>;
      },
    },
    {
      title: t('table.description'),
      dataIndex: 'description',
      key: 'description',
      width: 260,
      ellipsis: true,
      render: (text: string) => (
        <Tooltip title={text}>
          <span
            style={{
              display: 'block',
              maxWidth: 240,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              color: token.colorTextSecondary,
              fontSize: 13,
            }}
          >
            {text}
          </span>
        </Tooltip>
      ),
    },
    {
      title: t('table.triggers'),
      dataIndex: 'triggers',
      key: 'triggers',
      width: 180,
      render: (triggers: string[]) => (
        <Space wrap size={4}>
          {triggers.slice(0, 3).map((word, i) => (
            <Tag key={i} style={{ fontSize: 11 }}>{word}</Tag>
          ))}
          {triggers.length > 3 && <Tag>+{triggers.length - 3}</Tag>}
        </Space>
      ),
    },
    {
      title: t('table.actions'),
      key: 'actions',
      width: 200,
      render: (_: unknown, record: SkillItem) => (
        <Space>
          <Button
            type="text"
            icon={<FileTextOutlined />}
            onClick={() => handleViewRaw(record)}
            size="small"
          >
            {t('actions.view')}
          </Button>
          <Button
            type="text"
            icon={<EditOutlined />}
            onClick={() => handleEdit(record)}
            size="small"
          >
            {t('actions.edit')}
          </Button>
          <Popconfirm
            title={t('deleteConfirm.title')}
            description={t('deleteConfirm.description')}
            onConfirm={() => handleDelete(record.template_key)}
            okText={t('deleteConfirm.ok')}
            cancelText={t('deleteConfirm.cancel')}
            okButtonProps={{ danger: true }}
          >
            <Button type="text" danger icon={<DeleteOutlined />} size="small">
              {t('actions.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* 顶部标题栏 */}
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        marginBottom: 16,
        flexWrap: 'wrap',
        gap: 12,
      }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 20 }}>
            <ThunderboltOutlined style={{ marginRight: 8, color: token.colorPrimary }} />
            {t('page.title')}
            <Badge count={skills.length} style={{ marginLeft: 8, backgroundColor: token.colorPrimary }} />
          </h2>
          <div style={{ fontSize: 12, color: token.colorTextSecondary, marginTop: 4 }}>
            {t('page.subtitle')}
          </div>
        </div>
        <Space wrap>
          <Button
            icon={<ReloadOutlined />}
            onClick={loadSkills}
            loading={loading}
          >
            {t('actions.refresh')}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              createForm.resetFields();
              setCreateModalVisible(true);
            }}
          >
            {t('actions.addSkill')}
          </Button>
        </Space>
      </div>

      {/* Skill 列表 */}
      {skills.length === 0 && !loading ? (
        <Card>
          <Empty description={t('empty.noSkills')}>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateModalVisible(true)}>
              {t('actions.addSkill')}
            </Button>
          </Empty>
        </Card>
      ) : (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <Table
            dataSource={skills}
            columns={columns}
            rowKey="template_key"
            loading={loading}
            pagination={false}
            size="middle"
            style={{ background: token.colorBgContainer }}
          />
        </div>
      )}

      {/* 查看原始内容弹窗 */}
      <Modal
        title={t('viewModal.title')}
        open={viewModalVisible}
        onCancel={() => setViewModalVisible(false)}
        width={800}
        footer={<Button onClick={() => setViewModalVisible(false)}>{t('actions.close')}</Button>}
        styles={{ body: { maxHeight: '60vh', overflowY: 'auto' } }}
      >
        <pre style={{
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          fontSize: 13,
          lineHeight: 1.6,
          background: token.colorFillQuaternary,
          padding: 16,
          borderRadius: 8,
        }}>
          {viewingContent}
        </pre>
      </Modal>

      {/* 编辑 Skill 弹窗 */}
      <Modal
        title={t('editModal.title')}
        open={editModalVisible}
        onCancel={() => setEditModalVisible(false)}
        width={900}
        footer={
          <Space>
            <Button onClick={() => setEditModalVisible(false)}>{t('buttons.cancel')}</Button>
            <Button type="primary" onClick={handleSaveEdit} loading={saving}>{t('buttons.save')}</Button>
          </Space>
        }
        styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
        destroyOnHidden
      >
        <Form form={editForm} layout="vertical">
          <Form.Item label={t('form.internalName')} name="name" tooltip={t('form.internalNameTooltip')}>
            <Input disabled />
          </Form.Item>
          <Form.Item label={t('form.displayName')} name="display_name" rules={[{ required: true, whitespace: true, message: t('form.displayNameRequired') }]}
            tooltip={t('form.displayNameTooltip')}>
            <Input placeholder={t('form.displayNamePlaceholderEdit')} maxLength={60} />
          </Form.Item>
          <Form.Item label={t('form.category')} name="category" rules={[{ required: true, message: t('form.categorySelectRequired') }]}
            tooltip={t('form.categoryTooltip')}>
            <Select
              showSearch
              options={SKILL_CATEGORY_OPTIONS}
              placeholder={t('form.categoryPlaceholder')}
              mode="tags"
              maxCount={1}
              tokenSeparators={[',', '，', '、']}
            />
          </Form.Item>
          <Form.Item label={t('form.description')} name="description" rules={[{ required: true, whitespace: true, message: t('form.descriptionRequired') }]}
            tooltip={t('form.descriptionTooltip')}>
            <TextArea rows={4} placeholder={t('form.descriptionPlaceholder')} />
          </Form.Item>
          <Form.Item label={t('form.triggers')} name="triggers" rules={[{ required: true, whitespace: true, message: t('form.triggersRequired') }]}
            tooltip={t('form.triggersTooltip')}>
            <TextArea rows={4} placeholder={t('form.triggersPlaceholderEdit')} />
          </Form.Item>
          <Form.Item label={t('form.body')} name="body" rules={[{ required: true, message: t('form.bodyRequired') }]}
            tooltip={t('form.bodyTooltipEdit')}>
            <TextArea rows={15} placeholder={t('form.bodyPlaceholderEdit')} style={{ fontFamily: 'monospace', fontSize: 13 }} />
          </Form.Item>
          <Form.Item label={t('form.references')} name="references"
            tooltip={t('form.referencesTooltipEdit')}>
            <TextArea rows={8} placeholder={t('form.referencesPlaceholderEdit')} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 创建 Skill 弹窗 */}
      <Modal
        title={t('createModal.title')}
        open={createModalVisible}
        onCancel={() => setCreateModalVisible(false)}
        width={900}
        footer={
          <Space>
            <Button onClick={() => setCreateModalVisible(false)}>{t('buttons.cancel')}</Button>
            <Button type="primary" onClick={handleCreate} loading={saving}>{t('buttons.create')}</Button>
          </Space>
        }
        styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
        destroyOnHidden
      >
        <Form form={createForm} layout="vertical">
          <Form.Item label={t('form.skillName')} name="name" rules={[{ required: true, message: t('form.skillNameRequired') }]}
            tooltip={t('form.skillNameTooltip')}>
            <Input placeholder="my-new-skill" />
          </Form.Item>
          <Form.Item label={t('form.displayName')} name="display_name" rules={[{ required: true, whitespace: true, message: t('form.displayNameRequired') }]}
            tooltip={t('form.displayNameTooltip')}>
            <Input placeholder={t('form.displayNamePlaceholderCreate')} maxLength={60} />
          </Form.Item>
          <Form.Item label={t('form.category')} name="category" rules={[{ required: true, message: t('form.categorySelectRequired') }]}
            tooltip={t('form.categoryTooltip')}>
            <Select
              showSearch
              options={SKILL_CATEGORY_OPTIONS}
              placeholder={t('form.categoryPlaceholder')}
              mode="tags"
              maxCount={1}
              tokenSeparators={[',', '，', '、']}
            />
          </Form.Item>
          <Form.Item label={t('form.description')} name="description" rules={[{ required: true, whitespace: true, message: t('form.descriptionRequired') }]}
            tooltip={t('form.descriptionTooltip')}>
            <TextArea rows={4} placeholder={t('form.descriptionPlaceholder')} />
          </Form.Item>
          <Form.Item label={t('form.triggers')} name="triggers" rules={[{ required: true, whitespace: true, message: t('form.triggersRequired') }]}
            tooltip={t('form.triggersTooltip')}>
            <TextArea rows={4} placeholder={t('form.triggersPlaceholderCreate')} />
          </Form.Item>
          <Form.Item label={t('form.body')} name="body" rules={[{ required: true, message: t('form.bodyRequired') }]}
            tooltip={t('form.bodyTooltipCreate')}>
            <TextArea rows={15} placeholder={t('form.bodyPlaceholderCreate')} style={{ fontFamily: 'monospace', fontSize: 13 }} />
          </Form.Item>
          <Form.Item label={t('form.referencesOptional')} name="references"
            tooltip={t('form.referencesTooltipCreate')}>
            <TextArea rows={8} placeholder={t('form.referencesPlaceholderCreate')} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
