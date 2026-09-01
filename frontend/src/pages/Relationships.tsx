import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Card, Table, Tag, Button, Space, message, Modal, Form, Select, Slider, Input, Tabs, theme } from 'antd';
import { PlusOutlined, ApartmentOutlined, UserOutlined, EditOutlined } from '@ant-design/icons';
import { useStore } from '../store';
import axios from 'axios';

const { TextArea } = Input;

interface Relationship {
  id: string;
  character_from_id: string;
  character_to_id: string;
  relationship_name: string;
  relationship_type_names?: string[];
  intimacy_level: number;
  status?: string;
  description?: string;
  source: string;
}

interface RelationshipType {
  id: number;
  name: string;
  category: string;
  reverse_name?: string;
  icon?: string;
  description?: string;
  is_system?: boolean;
  source?: string;
  project_id?: string | null;
}

interface Character {
  id: string;
  name: string;
  is_organization: boolean;
}

export default function Relationships() {
  const { projectId } = useParams<{ projectId: string }>();
  const { currentProject } = useStore();
  const navigate = useNavigate();
  const [relationships, setRelationships] = useState<Relationship[]>([]);
  const [relationshipTypes, setRelationshipTypes] = useState<RelationshipType[]>([]);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [loading, setLoading] = useState(false);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [isEditMode, setIsEditMode] = useState(false);
  const [editingRelationship, setEditingRelationship] = useState<Relationship | null>(null);
  const [form] = Form.useForm();
  const [modal, contextHolder] = Modal.useModal();
  const { token } = theme.useToken();
  const [isMobile, setIsMobile] = useState(window.innerWidth <= 768);
  const [pageSize, setPageSize] = useState(10);
  const [currentPage, setCurrentPage] = useState(1);
  const [isTypeModalOpen, setIsTypeModalOpen] = useState(false);
  const [editingType, setEditingType] = useState<RelationshipType | null>(null);
  const [typeForm] = Form.useForm();

  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth <= 768);
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  useEffect(() => {
    if (projectId) {
      loadData();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const loadData = async () => {
    setLoading(true);
    try {
      const [relsRes, typesRes, charsRes] = await Promise.all([
        axios.get(`/api/relationships/project/${projectId}`),
        axios.get(`/api/relationships/types?project_id=${projectId}`),
        axios.get(`/api/characters?project_id=${projectId}`)
      ]);

      setRelationships(relsRes.data);
      setRelationshipTypes(typesRes.data);
      setCharacters(charsRes.data.items || []);
    } catch (error) {
      message.error('加载数据失败');
      console.error(error);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateRelationship = async (values: {
    character_from_id: string;
    character_to_id: string;
    relationship_type_names: string[];
    intimacy_level: number;
    status: string;
    description?: string;
  }) => {
    try {
      await axios.post('/api/relationships/', {
        project_id: projectId,
        ...values,
        relationship_name: values.relationship_type_names?.join('、') || '',
      });
      message.success('关系创建成功');
      setIsModalOpen(false);
      form.resetFields();
      loadData();
    } catch (error) {
      message.error('创建关系失败');
      console.error(error);
    }
  };

  const handleEditRelationship = (record: Relationship) => {
    setEditingRelationship(record);
    setIsEditMode(true);
    form.setFieldsValue({
      character_from_id: record.character_from_id,
      character_to_id: record.character_to_id,
      relationship_type_names: record.relationship_type_names || (record.relationship_name ? [record.relationship_name] : []),
      intimacy_level: record.intimacy_level,
      status: record.status || 'active',
      description: record.description,
    });
    setIsModalOpen(true);
  };

  const handleUpdateRelationship = async (values: {
    character_from_id: string;
    character_to_id: string;
    relationship_type_names: string[];
    intimacy_level: number;
    status: string;
    description?: string;
  }) => {
    if (!editingRelationship) return;

    try {
      await axios.put(`/api/relationships/${editingRelationship.id}`, {
        relationship_type_names: values.relationship_type_names,
        relationship_name: values.relationship_type_names?.join('、') || '',
        intimacy_level: values.intimacy_level,
        status: values.status,
        description: values.description,
      });
      message.success('关系更新成功');
      setIsModalOpen(false);
      setIsEditMode(false);
      setEditingRelationship(null);
      form.resetFields();
      loadData();
    } catch (error) {
      message.error('更新关系失败');
      console.error(error);
    }
  };

  const handleDeleteRelationship = async (id: string) => {
    modal.confirm({
      title: '确认删除',
      content: '确定要删除这条关系吗？',
      centered: true,
      okText: '删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await axios.delete(`/api/relationships/${id}`);
          message.success('关系删除成功');
          loadData();
        } catch (error) {
          message.error('删除失败');
          console.error(error);
        }
      }
    });
  };

  const openCreateTypeModal = () => {
    setEditingType(null);
    typeForm.resetFields();
    setIsTypeModalOpen(true);
  };

  const openEditTypeModal = (type: RelationshipType) => {
    setEditingType(type);
    typeForm.setFieldsValue({
      name: type.name,
      category: type.category,
      reverse_name: type.reverse_name,
      description: type.description,
      icon: type.icon,
    });
    setIsTypeModalOpen(true);
  };

  const handleSaveType = async (values: { name: string; category: string; reverse_name?: string; description?: string; icon?: string }) => {
    try {
      if (editingType) {
        await axios.put(`/api/relationships/types/${editingType.id}`, values);
      } else {
        await axios.post('/api/relationships/types', { project_id: projectId, ...values });
      }
      message.success(editingType ? '关系类型已更新' : '关系类型已创建');
      setIsTypeModalOpen(false);
      setEditingType(null);
      typeForm.resetFields();
      loadData();
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '保存关系类型失败');
      console.error(error);
    }
  };

  const handleDeleteType = async (type: RelationshipType) => {
    modal.confirm({
      title: '确认删除',
      content: `确定要删除关系类型「${type.name}」吗？`,
      centered: true,
      okText: '删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await axios.delete(`/api/relationships/types/${type.id}`);
          message.success('关系类型删除成功');
          loadData();
        } catch (error: any) {
          message.error(error?.response?.data?.detail || '删除失败');
          console.error(error);
        }
      }
    });
  };

  const getCharacterName = (id: string) => {
    const char = characters.find(c => c.id === id);
    return char?.name || '未知';
  };

  const getIntimacyColor = (level: number) => {
    if (level >= 75) return 'green';
    if (level >= 50) return 'blue';
    if (level >= 25) return 'orange';
    if (level >= 0) return 'volcano';
    return 'red';
  };

  const getCategoryColor = (category: string) => {
    const colors: Record<string, string> = {
      family: 'magenta',
      social: 'blue',
      hostile: 'red',
      professional: 'cyan'
    };
    return colors[category] || 'default';
  };

  const columns = [
    {
      title: '角色A',
      dataIndex: 'character_from_id',
      key: 'from',
      render: (id: string) => (
        <Tag icon={<UserOutlined />} color="blue">
          {getCharacterName(id)}
        </Tag>
      ),
      width: 120,
    },
    {
      title: '关系',
      dataIndex: 'relationship_name',
      key: 'relationship',
      render: (_: string, record: Relationship) => (
        <Space size={4} wrap>
          {(record.relationship_type_names?.length ? record.relationship_type_names : [record.relationship_name])
            .filter(Boolean)
            .map(name => <Tag key={name} color="blue">{name}</Tag>)}
        </Space>
      ),
      width: 200,
    },
    {
      title: '角色B',
      dataIndex: 'character_to_id',
      key: 'to',
      render: (id: string) => (
        <Tag icon={<UserOutlined />} color="purple">
          {getCharacterName(id)}
        </Tag>
      ),
      width: 120,
    },
    {
      title: '亲密度',
      dataIndex: 'intimacy_level',
      key: 'intimacy',
      render: (level: number) => (
        <Tag color={getIntimacyColor(level)}>{level}</Tag>
      ),
      width: 80,
    },
    {
      title: '来源',
      dataIndex: 'source',
      key: 'source',
      render: (source: string) => (
        <Tag>{source === 'ai' ? 'AI生成' : '手动创建'}</Tag>
      ),
      width: 100,
    },
    {
      title: '操作',
      key: 'action',
      render: (_: unknown, record: Relationship) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEditRelationship(record)}
          >
            编辑
          </Button>
          <Button
            type="link"
            danger
            size="small"
            onClick={() => handleDeleteRelationship(record.id)}
          >
            删除
          </Button>
        </Space>
      ),
      width: 140,
      fixed: isMobile ? ('right' as const) : undefined,
    },
  ];

  // 按类别分组关系类型
  const groupedTypes = relationshipTypes.reduce((acc, type) => {
    if (!acc[type.category]) {
      acc[type.category] = [];
    }
    acc[type.category].push(type);
    return acc;
  }, {} as Record<string, RelationshipType[]>);

  // 每行 = 一条关系记录；同一对角色允许多条记录，各自独立展示与编辑。
  // 类型以独立 Tag 渲染（一个关系类型一个标签），不再合并成单个字符串。
  const categoryLabels: Record<string, string> = {
    family: '家族关系',
    social: '社交关系',
    professional: '职业关系',
    hostile: '敌对关系'
  };

  return (
    <>
      {contextHolder}
      <div>
        <Card
        title={
          <Space wrap>
            <ApartmentOutlined />
            <span style={{ fontSize: isMobile ? 14 : 16 }}>关系管理</span>
            {!isMobile && <Tag color="blue">{currentProject?.title}</Tag>}
          </Space>
        }
        extra={
          <Space>
            <Button
              onClick={() => projectId && navigate(`/project/${projectId}/relationships-graph`)}
              size={isMobile ? 'small' : 'middle'}
            >
              关系图谱
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setIsModalOpen(true)}
              size={isMobile ? 'small' : 'middle'}
            >
              {isMobile ? '添加' : '添加关系'}
            </Button>
          </Space>
        }
      >
        <Tabs
          items={[
            {
              key: 'list',
              label: `关系列表 (${relationships.length})`,
              children: (
                <Table
                  columns={columns}
                  dataSource={relationships}
                  rowKey="id"
                  loading={loading}
                  pagination={{
                    current: currentPage,
                    pageSize: isMobile ? 10 : pageSize,
                    pageSizeOptions: ['10', '20', '50', '100'],
                    position: ['bottomCenter'],
                    showSizeChanger: !isMobile,
                    showQuickJumper: !isMobile,
                    showTotal: (total) => `共 ${total} 条`,
                    simple: isMobile,
                    onChange: (page, size) => {
                      setCurrentPage(page);
                      if (size !== pageSize) {
                        setPageSize(size);
                        setCurrentPage(1);
                      }
                    },
                    onShowSizeChange: (_, size) => {
                      setPageSize(size);
                      setCurrentPage(1);
                    }
                  }}
                  scroll={{
                    x: 700,
                    y: isMobile ? 'calc(100vh - 360px)' : 'calc(100vh - 440px)'
                  }}
                  size={isMobile ? 'small' : 'middle'}
                />
              ),
            },
            {
              key: 'types',
              label: (
                <Space>
                  <span>关系类型 ({relationshipTypes.length})</span>
                  <Button size="small" type="primary" icon={<PlusOutlined />} onClick={openCreateTypeModal}>
                    新增
                  </Button>
                </Space>
              ),
              children: (
                <div style={{
                  display: 'grid',
                  gridTemplateColumns: isMobile ? '1fr' : 'repeat(auto-fill, minmax(200px, 1fr))',
                  gap: isMobile ? '12px' : '16px',
                  maxHeight: isMobile ? 'calc(100vh - 400px)' : 'calc(100vh - 350px)',
                  overflow: 'auto'
                }}>
                  {Object.entries(groupedTypes).map(([category, types]) => (
                    <Card
                      key={category}
                      size="small"
                      title={categoryLabels[category] || category}
                      headStyle={{ backgroundColor: token.colorFillAlter }}
                    >
                      <Space direction="vertical" style={{ width: '100%' }}>
                        {types.map(type => (
                          <Space key={type.id} style={{ width: '100%', justifyContent: 'space-between' }}>
                            <Tag color={getCategoryColor(category)}>
                              {type.icon} {type.name}
                              {type.reverse_name && ` ↔ ${type.reverse_name}`}
                              {!type.is_system && <span style={{ marginLeft: 4, color: 'gray' }}>项目</span>}
                            </Tag>
                            {!type.is_system && (
                              <Space size={4}>
                                <Button size="small" type="link" icon={<EditOutlined />} onClick={() => openEditTypeModal(type)} />
                                <Button size="small" type="link" danger onClick={() => handleDeleteType(type)}>删除</Button>
                              </Space>
                            )}
                          </Space>
                        ))}
                      </Space>
                    </Card>
                  ))}
                </div>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={isEditMode ? '编辑关系' : '添加关系'}
        open={isModalOpen}
        onCancel={() => {
          setIsModalOpen(false);
          setIsEditMode(false);
          setEditingRelationship(null);
          form.resetFields();
        }}
        footer={null}
        centered={!isMobile}
        width={isMobile ? '100%' : 600}
        style={isMobile ? { top: 0, paddingBottom: 0, maxWidth: '100vw' } : undefined}
        styles={isMobile ? { body: { maxHeight: 'calc(100vh - 110px)', overflowY: 'auto' } } : undefined}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={isEditMode ? handleUpdateRelationship : handleCreateRelationship}
        >
          <Form.Item
            name="character_from_id"
            label="角色A"
            rules={[{ required: true, message: '请选择角色A' }]}
          >
            <Select
              placeholder="选择角色"
              showSearch
              disabled={isEditMode}
              filterOption={(input, option) =>
                (option?.label ?? '').toLowerCase().includes(input.toLowerCase())
              }
              options={characters
                .filter(c => !c.is_organization)
                .map(c => ({ label: c.name, value: c.id }))}
            />
          </Form.Item>

          <Form.Item
            name="relationship_type_names"
            label="关系类型（可多选）"
            rules={[{ required: true, message: '请至少选择一个关系类型' }]}
          >
            <Select
              mode="tags"
              placeholder="选择已有类型或输入新类型"
              options={relationshipTypes.map(t => ({
                label: `${t.icon || ''} ${t.name}${t.is_system ? '' : '（项目）'}`,
                value: t.name
              }))}
            />
          </Form.Item>

          <Form.Item
            name="character_to_id"
            label="角色B"
            rules={[{ required: true, message: '请选择角色B' }]}
          >
            <Select
              placeholder="选择角色"
              showSearch
              disabled={isEditMode}
              filterOption={(input, option) =>
                (option?.label ?? '').toLowerCase().includes(input.toLowerCase())
              }
              options={characters
                .filter(c => !c.is_organization)
                .map(c => ({ label: c.name, value: c.id }))}
            />
          </Form.Item>

          <Form.Item
            name="intimacy_level"
            label="亲密度"
            initialValue={50}
          >
            <Slider
              min={-100}
              max={100}
              marks={{
                '-100': '-100',
                '-50': '-50',
                0: '0',
                50: '50',
                100: '100'
              }}
            />
          </Form.Item>

          <Form.Item
            name="status"
            label="状态"
            initialValue="active"
          >
            <Select>
              <Select.Option value="active">活跃</Select.Option>
              <Select.Option value="broken">破裂</Select.Option>
              <Select.Option value="past">过去</Select.Option>
              <Select.Option value="complicated">复杂</Select.Option>
            </Select>
          </Form.Item>

          <Form.Item name="description" label="关系描述">
            <TextArea rows={3} placeholder="描述这段关系的细节..." />
          </Form.Item>

          <Form.Item>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setIsModalOpen(false);
                setIsEditMode(false);
                setEditingRelationship(null);
                form.resetFields();
              }}>取消</Button>
              <Button type="primary" htmlType="submit">
                {isEditMode ? '更新' : '创建'}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editingType ? '编辑关系类型' : '新增关系类型'}
        open={isTypeModalOpen}
        onCancel={() => {
          setIsTypeModalOpen(false);
          setEditingType(null);
          typeForm.resetFields();
        }}
        footer={null}
        centered={!isMobile}
        width={isMobile ? '100%' : 480}
      >
        <Form form={typeForm} layout="vertical" onFinish={handleSaveType}>
          <Form.Item name="name" label="类型名称" rules={[{ required: true, message: '请输入类型名称' }]}>
            <Input maxLength={50} />
          </Form.Item>
          <Form.Item name="category" label="分类" initialValue="custom">
            <Select options={[
              { label: '自定义', value: 'custom' },
              { label: '家族关系', value: 'family' },
              { label: '社交关系', value: 'social' },
              { label: '职业关系', value: 'professional' },
              { label: '敌对关系', value: 'hostile' },
            ]} />
          </Form.Item>
          <Form.Item name="reverse_name" label="反向名称">
            <Input maxLength={50} />
          </Form.Item>
          <Form.Item name="icon" label="图标">
            <Input maxLength={50} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <TextArea rows={2} />
          </Form.Item>
          <Form.Item>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setIsTypeModalOpen(false);
                setEditingType(null);
                typeForm.resetFields();
              }}>取消</Button>
              <Button type="primary" htmlType="submit">保存</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
      </div>
    </>
  );
}
