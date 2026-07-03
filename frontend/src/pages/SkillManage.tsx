import { useState, useEffect } from 'react';
import { Button, Table, Modal, Form, Input, Tag, Space, message, Popconfirm, Card, theme, Empty, Badge, Tooltip, Select } from 'antd';
import { PlusOutlined, EditOutlined, DeleteOutlined, ReloadOutlined, ThunderboltOutlined, FileTextOutlined } from '@ant-design/icons';

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
      message.error('加载 Skill 列表失败');
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
        message.error('获取 Skill 详情失败');
      }
    } catch {
      message.error('获取 Skill 详情失败');
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
      message.error('获取内容失败');
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
          message.error('参考资料 JSON 格式错误');
          setSaving(false);
          return;
        }
      }

      const triggers = parseTriggers(values.triggers);
      if (triggers.length === 0) {
        message.error('请至少填写一个触发词');
        setSaving(false);
        return;
      }
      const category = normalizeCategory(values.category);
      if (!category) {
        message.error('请选择或输入分类');
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
        message.success('Skill 更新成功');
        setEditModalVisible(false);
        loadSkills();
      } else {
        const err = await response.json();
        message.error(err.detail || '更新失败');
      }
    } catch {
      message.error('保存失败');
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
          message.error('参考资料 JSON 格式错误');
          setSaving(false);
          return;
        }
      }

      const triggers = parseTriggers(values.triggers);
      if (triggers.length === 0) {
        message.error('请至少填写一个触发词');
        setSaving(false);
        return;
      }
      const category = normalizeCategory(values.category);
      if (!category) {
        message.error('请选择或输入分类');
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
        message.success('Skill 创建成功');
        setCreateModalVisible(false);
        createForm.resetFields();
        loadSkills();
      } else {
        const err = await response.json();
        message.error(err.detail || '创建失败');
      }
    } catch {
      message.error('创建失败');
    } finally {
      setSaving(false);
    }
  };

  // 删除 Skill
  const handleDelete = async (skillKey: string) => {
    try {
      const response = await fetch(`/api/skills/delete/${skillKey}`, { method: 'DELETE' });
      if (response.ok) {
        message.success('删除成功');
        loadSkills();
      } else {
        const err = await response.json();
        message.error(err.detail || '删除失败');
      }
    } catch {
      message.error('删除失败');
    }
  };

  const columns = [
    {
      title: '名称',
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
      title: '分类',
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
      title: '描述',
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
      title: '触发词',
      dataIndex: 'triggers',
      key: 'triggers',
      width: 180,
      render: (triggers: string[]) => (
        <Space wrap size={4}>
          {triggers.slice(0, 3).map((t, i) => (
            <Tag key={i} style={{ fontSize: 11 }}>{t}</Tag>
          ))}
          {triggers.length > 3 && <Tag>+{triggers.length - 3}</Tag>}
        </Space>
      ),
    },
    {
      title: '操作',
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
            查看
          </Button>
          <Button
            type="text"
            icon={<EditOutlined />}
            onClick={() => handleEdit(record)}
            size="small"
          >
            编辑
          </Button>
          <Popconfirm
            title="确定删除此 Skill？"
            description="删除后无法恢复，相关文件将被永久删除。"
            onConfirm={() => handleDelete(record.template_key)}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button type="text" danger icon={<DeleteOutlined />} size="small">
              删除
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
            Skill 管理
            <Badge count={skills.length} style={{ marginLeft: 8, backgroundColor: token.colorPrimary }} />
          </h2>
          <div style={{ fontSize: 12, color: token.colorTextSecondary, marginTop: 4 }}>
            在线管理 Skill 工作流，添加、编辑或删除
          </div>
        </div>
        <Space wrap>
          <Button
            icon={<ReloadOutlined />}
            onClick={loadSkills}
            loading={loading}
          >
            刷新
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              createForm.resetFields();
              setCreateModalVisible(true);
            }}
          >
            添加 Skill
          </Button>
        </Space>
      </div>

      {/* Skill 列表 */}
      {skills.length === 0 && !loading ? (
        <Card>
          <Empty description="暂无 Skill，点击「添加 Skill」创建">
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateModalVisible(true)}>
              添加 Skill
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
        title="SKILL.md 原始内容"
        open={viewModalVisible}
        onCancel={() => setViewModalVisible(false)}
        width={800}
        footer={<Button onClick={() => setViewModalVisible(false)}>关闭</Button>}
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
        title="编辑 Skill"
        open={editModalVisible}
        onCancel={() => setEditModalVisible(false)}
        width={900}
        footer={
          <Space>
            <Button onClick={() => setEditModalVisible(false)}>取消</Button>
            <Button type="primary" onClick={handleSaveEdit} loading={saving}>保存</Button>
          </Space>
        }
        styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
        destroyOnClose
      >
        <Form form={editForm} layout="vertical">
          <Form.Item label="内部标识" name="name" tooltip="来自 SKILL.md 的 name 字段，编辑时不支持修改">
            <Input disabled />
          </Form.Item>
          <Form.Item label="显示名称" name="display_name" rules={[{ required: true, whitespace: true, message: '请输入显示名称' }]}
            tooltip="表格和工具箱中展示的名称">
            <Input placeholder="例如：长篇网文拆文" maxLength={60} />
          </Form.Item>
          <Form.Item label="分类" name="category" rules={[{ required: true, message: '请选择分类' }]}
            tooltip="表格和工具箱中展示的 Skill 分类">
            <Select
              showSearch
              options={SKILL_CATEGORY_OPTIONS}
              placeholder="请选择或输入分类"
              mode="tags"
              maxCount={1}
              tokenSeparators={[',', '，', '、']}
            />
          </Form.Item>
          <Form.Item label="描述" name="description" rules={[{ required: true, whitespace: true, message: '请输入描述' }]}
            tooltip="用于解释 Skill 的用途，不再承担名称和触发词配置">
            <TextArea rows={4} placeholder="简要描述 Skill 功能、适用场景和使用方式..." />
          </Form.Item>
          <Form.Item label="触发词" name="triggers" rules={[{ required: true, whitespace: true, message: '请至少填写一个触发词' }]}
            tooltip="每行一个，也支持用逗号、顿号分隔。建议包含 /skill-name">
            <TextArea rows={4} placeholder={'/story-long-analyze\n/长篇拆文\n帮我拆这本书'} />
          </Form.Item>
          <Form.Item label="工作流指令" name="body" rules={[{ required: true, message: '请输入工作流指令' }]}
            tooltip="SKILL.md 中 YAML frontmatter 之后的 Markdown 正文">
            <TextArea rows={15} placeholder="输入 Skill 的完整工作流指令..." style={{ fontFamily: 'monospace', fontSize: 13 }} />
          </Form.Item>
          <Form.Item label="参考资料 (JSON)" name="references"
            tooltip='格式：{"文件名": "内容"}。留空则保留原有参考资料'>
            <TextArea rows={8} placeholder='{"anti-ai-tips": "去AI味的技巧...", "quality-check": "质量检查清单..."}' style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 创建 Skill 弹窗 */}
      <Modal
        title="添加新 Skill"
        open={createModalVisible}
        onCancel={() => setCreateModalVisible(false)}
        width={900}
        footer={
          <Space>
            <Button onClick={() => setCreateModalVisible(false)}>取消</Button>
            <Button type="primary" onClick={handleCreate} loading={saving}>创建</Button>
          </Space>
        }
        styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
        destroyOnClose
      >
        <Form form={createForm} layout="vertical">
          <Form.Item label="Skill 名称（英文）" name="name" rules={[{ required: true, message: '请输入名称' }]}
            tooltip="英文小写+短横线，如 my-new-skill。将作为目录名和内部标识">
            <Input placeholder="my-new-skill" />
          </Form.Item>
          <Form.Item label="显示名称" name="display_name" rules={[{ required: true, whitespace: true, message: '请输入显示名称' }]}
            tooltip="表格和工具箱中展示的名称">
            <Input placeholder="例如：我的新 Skill" maxLength={60} />
          </Form.Item>
          <Form.Item label="分类" name="category" rules={[{ required: true, message: '请选择分类' }]}
            tooltip="表格和工具箱中展示的 Skill 分类">
            <Select
              showSearch
              options={SKILL_CATEGORY_OPTIONS}
              placeholder="请选择或输入分类"
              mode="tags"
              maxCount={1}
              tokenSeparators={[',', '，', '、']}
            />
          </Form.Item>
          <Form.Item label="描述" name="description" rules={[{ required: true, whitespace: true, message: '请输入描述' }]}
            tooltip="用于解释 Skill 的用途，不再承担名称和触发词配置">
            <TextArea rows={4} placeholder="简要描述 Skill 功能、适用场景和使用方式..." />
          </Form.Item>
          <Form.Item label="触发词" name="triggers" rules={[{ required: true, whitespace: true, message: '请至少填写一个触发词' }]}
            tooltip="每行一个，也支持用逗号、顿号分隔。建议包含 /skill-name">
            <TextArea rows={4} placeholder={'/my-new-skill\n我的新 Skill'} />
          </Form.Item>
          <Form.Item label="工作流指令" name="body" rules={[{ required: true, message: '请输入工作流指令' }]}
            tooltip="Skill 的核心 Markdown 内容">
            <TextArea rows={15} placeholder={"# my-new-skill：Skill 标题\n\n你是 xxx 专家。你的任务是帮用户完成 xxx。\n\n## 核心原则\n\n- 原则1...\n\n## 工作流程\n\n### Phase 1：需求确认\n..."} style={{ fontFamily: 'monospace', fontSize: 13 }} />
          </Form.Item>
          <Form.Item label="参考资料 (JSON，可选)" name="references"
            tooltip='格式：{"文件名": "内容"}'>
            <TextArea rows={8} placeholder='{"tips": "参考技巧...", "examples": "示例..."}' style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
