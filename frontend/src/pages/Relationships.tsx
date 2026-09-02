import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Card, Table, Tag, Button, Space, message, Modal, Form, Select, Slider, Input, Tabs, theme } from 'antd';
import { PlusOutlined, ApartmentOutlined, UserOutlined, EditOutlined } from '@ant-design/icons';
import { useStore } from '../store';
import axios from 'axios';
import { useTranslation } from 'react-i18next';

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
  const { t } = useTranslation('relationships');
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
      message.error(t('toast.loadDataFailed'));
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
      message.success(t('toast.createSuccess'));
      setIsModalOpen(false);
      form.resetFields();
      loadData();
    } catch (error) {
      message.error(t('toast.createFailed'));
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
      message.success(t('toast.updateSuccess'));
      setIsModalOpen(false);
      setIsEditMode(false);
      setEditingRelationship(null);
      form.resetFields();
      loadData();
    } catch (error) {
      message.error(t('toast.updateFailed'));
      console.error(error);
    }
  };

  const handleDeleteRelationship = async (id: string) => {
    modal.confirm({
      title: t('delete.title'),
      content: t('delete.content'),
      centered: true,
      okText: t('delete.ok'),
      okType: 'danger',
      cancelText: t('delete.cancel'),
      onOk: async () => {
        try {
          await axios.delete(`/api/relationships/${id}`);
          message.success(t('toast.deleteSuccess'));
          loadData();
        } catch (error) {
          message.error(t('toast.deleteFailed'));
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
      message.success(editingType ? t('toast.typeUpdated') : t('toast.typeCreated'));
      setIsTypeModalOpen(false);
      setEditingType(null);
      typeForm.resetFields();
      loadData();
    } catch (error: any) {
      message.error(error?.response?.data?.detail || t('toast.saveTypeFailed'));
      console.error(error);
    }
  };

  const handleDeleteType = async (type: RelationshipType) => {
    modal.confirm({
      title: t('delete.title'),
      content: t('delete.typeContent', { name: type.name }),
      centered: true,
      okText: t('delete.ok'),
      okType: 'danger',
      cancelText: t('delete.cancel'),
      onOk: async () => {
        try {
          await axios.delete(`/api/relationships/types/${type.id}`);
          message.success(t('toast.typeDeleteSuccess'));
          loadData();
        } catch (error: any) {
          message.error(error?.response?.data?.detail || t('toast.deleteFailed'));
          console.error(error);
        }
      }
    });
  };

  const getCharacterName = (id: string) => {
    const char = characters.find(c => c.id === id);
    return char?.name || t('unknown');
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
      title: t('table.characterA'),
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
      title: t('table.relationship'),
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
      title: t('table.characterB'),
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
      title: t('table.intimacy'),
      dataIndex: 'intimacy_level',
      key: 'intimacy',
      render: (level: number) => (
        <Tag color={getIntimacyColor(level)}>{level}</Tag>
      ),
      width: 80,
    },
    {
      title: t('table.source'),
      dataIndex: 'source',
      key: 'source',
      render: (source: string) => (
        <Tag>{source === 'ai' ? t('source.ai') : t('source.manual')}</Tag>
      ),
      width: 100,
    },
    {
      title: t('table.actions'),
      key: 'action',
      render: (_: unknown, record: Relationship) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEditRelationship(record)}
          >
            {t('actions.edit')}
          </Button>
          <Button
            type="link"
            danger
            size="small"
            onClick={() => handleDeleteRelationship(record.id)}
          >
            {t('actions.delete')}
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
    family: t('category.family'),
    social: t('category.social'),
    professional: t('category.professional'),
    hostile: t('category.hostile')
  };

  return (
    <>
      {contextHolder}
      <div>
        <Card
        title={
          <Space wrap>
            <ApartmentOutlined />
            <span style={{ fontSize: isMobile ? 14 : 16 }}>{t('page.title')}</span>
            {!isMobile && <Tag color="blue">{currentProject?.title}</Tag>}
          </Space>
        }
        extra={
          <Space>
            <Button
              onClick={() => projectId && navigate(`/project/${projectId}/relationships-graph`)}
              size={isMobile ? 'small' : 'middle'}
            >
              {t('page.graph')}
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setIsModalOpen(true)}
              size={isMobile ? 'small' : 'middle'}
            >
              {isMobile ? t('actions.add') : t('actions.addRelationship')}
            </Button>
          </Space>
        }
      >
        <Tabs
          items={[
            {
              key: 'list',
              label: t('tabs.list', { n: relationships.length }),
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
                    showTotal: (total) => t('table.total', { n: total }),
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
                  <span>{t('tabs.types', { n: relationshipTypes.length })}</span>
                  <Button size="small" type="primary" icon={<PlusOutlined />} onClick={openCreateTypeModal}>
                    {t('actions.add')}
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
                              {!type.is_system && <span style={{ marginLeft: 4, color: 'gray' }}>{t('type.project')}</span>}
                            </Tag>
                            {!type.is_system && (
                              <Space size={4}>
                                <Button size="small" type="link" icon={<EditOutlined />} onClick={() => openEditTypeModal(type)} />
                                <Button size="small" type="link" danger onClick={() => handleDeleteType(type)}>{t('actions.delete')}</Button>
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
        title={isEditMode ? t('modal.editTitle') : t('modal.addTitle')}
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
            label={t('form.characterA')}
            rules={[{ required: true, message: t('form.characterARequired') }]}
          >
            <Select
              placeholder={t('form.selectCharacter')}
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
            label={t('form.typeNames')}
            rules={[{ required: true, message: t('form.typeNamesRequired') }]}
          >
            <Select
              mode="tags"
              placeholder={t('form.typeNamesPlaceholder')}
              options={relationshipTypes.map(rt => ({
                label: `${rt.icon || ''} ${rt.name}${rt.is_system ? '' : t('form.typeProjectSuffix')}`,
                value: rt.name
              }))}
            />
          </Form.Item>

          <Form.Item
            name="character_to_id"
            label={t('form.characterB')}
            rules={[{ required: true, message: t('form.characterBRequired') }]}
          >
            <Select
              placeholder={t('form.selectCharacter')}
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
            label={t('form.intimacy')}
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
            label={t('form.status')}
            initialValue="active"
          >
            <Select>
              <Select.Option value="active">{t('status.active')}</Select.Option>
              <Select.Option value="broken">{t('status.broken')}</Select.Option>
              <Select.Option value="past">{t('status.past')}</Select.Option>
              <Select.Option value="complicated">{t('status.complicated')}</Select.Option>
            </Select>
          </Form.Item>

          <Form.Item name="description" label={t('form.description')}>
            <TextArea rows={3} placeholder={t('form.descriptionPlaceholder')} />
          </Form.Item>

          <Form.Item>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setIsModalOpen(false);
                setIsEditMode(false);
                setEditingRelationship(null);
                form.resetFields();
              }}>{t('buttons.cancel')}</Button>
              <Button type="primary" htmlType="submit">
                {isEditMode ? t('buttons.update') : t('buttons.create')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editingType ? t('typeModal.editTitle') : t('typeModal.addTitle')}
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
          <Form.Item name="name" label={t('typeModal.name')} rules={[{ required: true, message: t('typeModal.nameRequired') }]}>
            <Input maxLength={50} />
          </Form.Item>
          <Form.Item name="category" label={t('typeModal.category')} initialValue="custom">
            <Select options={[
              { label: t('typeModal.catCustom'), value: 'custom' },
              { label: t('typeModal.catFamily'), value: 'family' },
              { label: t('typeModal.catSocial'), value: 'social' },
              { label: t('typeModal.catProfessional'), value: 'professional' },
              { label: t('typeModal.catHostile'), value: 'hostile' },
            ]} />
          </Form.Item>
          <Form.Item name="reverse_name" label={t('typeModal.reverseName')}>
            <Input maxLength={50} />
          </Form.Item>
          <Form.Item name="icon" label={t('typeModal.icon')}>
            <Input maxLength={50} />
          </Form.Item>
          <Form.Item name="description" label={t('typeModal.description')}>
            <TextArea rows={2} />
          </Form.Item>
          <Form.Item>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setIsTypeModalOpen(false);
                setEditingType(null);
                typeForm.resetFields();
              }}>{t('buttons.cancel')}</Button>
              <Button type="primary" htmlType="submit">{t('buttons.save')}</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
      </div>
    </>
  );
}
