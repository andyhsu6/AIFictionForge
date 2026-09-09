import { useState, useEffect, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import { Card, Table, Tag, Button, Space, message, Modal, Form, Select, InputNumber, Input, Descriptions, Drawer, theme } from 'antd';
import { PlusOutlined, UserOutlined, EditOutlined, DeleteOutlined, UnorderedListOutlined, BankOutlined } from '@ant-design/icons';
import { useStore } from '../store';
import { useCharacterSync } from '../store/hooks';
import axios from 'axios';
import { eventBus, EventNames } from '../store/eventBus';
import { useTranslation } from 'react-i18next';

interface Organization {
  id: string;
  character_id: string;
  name: string;
  type: string;
  purpose: string;
  member_count: number;
  power_level: number;
  location?: string;
  motto?: string;
  color?: string;
}

interface OrganizationMember {
  id: string;
  character_id: string;
  character_name: string;
  position: string;
  rank: number;
  loyalty: number;
  contribution: number;
  status: string;
  joined_at?: string;
  left_at?: string;
  notes?: string;
}

interface Character {
  id: string;
  name: string;
  is_organization: boolean;
}

export default function Organizations() {
  const { t } = useTranslation('organizations');
  const { projectId } = useParams<{ projectId: string }>();
  const { currentProject } = useStore();
  const { refreshCharacters } = useCharacterSync();
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [selectedOrg, setSelectedOrg] = useState<Organization | null>(null);
  const [members, setMembers] = useState<OrganizationMember[]>([]);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [loading, setLoading] = useState(false);
  const [isAddMemberModalOpen, setIsAddMemberModalOpen] = useState(false);
  const [isEditMemberModalOpen, setIsEditMemberModalOpen] = useState(false);
  const [isEditOrgModalOpen, setIsEditOrgModalOpen] = useState(false);
  const [editingMember, setEditingMember] = useState<OrganizationMember | null>(null);
  const [form] = Form.useForm();
  const [editMemberForm] = Form.useForm();
  const [editOrgForm] = Form.useForm();
  const [isMobile, setIsMobile] = useState(window.innerWidth <= 768);
  const [modal, contextHolder] = Modal.useModal();
  const [orgListVisible, setOrgListVisible] = useState(false);
  const { token } = theme.useToken();

  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth <= 768);
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  const loadOrganizations = useCallback(async () => {
    setLoading(true);
    try {
      const res = await axios.get(`/api/organizations/project/${projectId}`);
      setOrganizations(res.data);
      if (res.data.length > 0 && !selectedOrg) {
        setSelectedOrg(res.data[0]);
        loadMembers(res.data[0].id);
      }
    } catch (error) {
      message.error(t('toast.loadOrgFailed'));
      console.error(error);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const loadCharacters = useCallback(async () => {
    try {
      const res = await axios.get(`/api/characters?project_id=${projectId}`);
      setCharacters(res.data.items || []);
    } catch (error) {
      console.error('加载角色列表失败', error);
    }
  }, [projectId]);

  useEffect(() => {
    if (projectId) {
      loadOrganizations();
      loadCharacters();
    }
  }, [projectId, loadOrganizations, loadCharacters]);

  useEffect(() => {
    const handleTaskSettled = (payload?: unknown) => {
      if (!payload || typeof payload !== 'object') return;
      const data = payload as { projectId?: string; resources?: string[] };
      if (data.projectId && data.projectId !== projectId) return;
      if (!data.resources?.includes('organizations')) return;
      void loadOrganizations();
      void loadCharacters();
      if (selectedOrg?.id) void loadMembers(selectedOrg.id);
    };
    eventBus.on(EventNames.BACKGROUND_TASK_SETTLED, handleTaskSettled);
    return () => eventBus.off(EventNames.BACKGROUND_TASK_SETTLED, handleTaskSettled);
  }, [loadCharacters, loadOrganizations, projectId, selectedOrg?.id]);

  const loadMembers = async (orgId: string) => {
    try {
      const res = await axios.get(`/api/organizations/${orgId}/members`);
      setMembers(res.data);
    } catch (error) {
      message.error(t('toast.loadMembersFailed'));
      console.error(error);
    }
  };

  const handleSelectOrganization = (org: Organization) => {
    setSelectedOrg(org);
    loadMembers(org.id);
  };

  const handleAddMember = async (values: Record<string, unknown>) => {
    if (!selectedOrg) return;

    try {
      await axios.post(`/api/organizations/${selectedOrg.id}/members`, values);
      message.success(t('toast.memberAdded'));
      setIsAddMemberModalOpen(false);
      form.resetFields();
      loadMembers(selectedOrg.id);
      loadOrganizations(); // 刷新成员计数
    } catch (error) {
      message.error(t('toast.memberAddFailed'));
      console.error(error);
    }
  };

  const handleRemoveMember = async (memberId: string) => {
    modal.confirm({
      title: t('removeConfirm.title'),
      content: t('removeConfirm.content'),
      centered: true,
      okText: t('removeConfirm.ok'),
      okType: 'danger',
      cancelText: t('removeConfirm.cancel'),
      onOk: async () => {
        try {
          await axios.delete(`/api/organizations/members/${memberId}`);
          message.success(t('toast.memberRemoved'));
          if (selectedOrg) {
            loadMembers(selectedOrg.id);
            loadOrganizations(); // 刷新成员计数
          }
        } catch (error) {
          message.error(t('toast.memberRemoveFailed'));
          console.error(error);
        }
      }
    });
  };

  const handleEditMember = (member: OrganizationMember) => {
    setEditingMember(member);
    editMemberForm.setFieldsValue({
      position: member.position,
      rank: member.rank,
      loyalty: member.loyalty,
      contribution: member.contribution,
      status: member.status,
      notes: member.notes,
      joined_at: member.joined_at
    });
    setIsEditMemberModalOpen(true);
  };

  const handleUpdateMember = async (values: Record<string, unknown>) => {
    if (!editingMember) return;

    try {
      await axios.put(`/api/organizations/members/${editingMember.id}`, values);
      message.success(t('toast.memberUpdated'));
      setIsEditMemberModalOpen(false);
      editMemberForm.resetFields();
      setEditingMember(null);
      if (selectedOrg) {
        loadMembers(selectedOrg.id);
      }
    } catch (error) {
      message.error(t('toast.updateFailed'));
      console.error(error);
    }
  };

  const getStatusColor = (status: string) => {
    const colors: Record<string, string> = {
      active: 'green',
      retired: 'default',
      expelled: 'red',
      deceased: 'black'
    };
    return colors[status] || 'default';
  };

  const getStatusText = (status: string) => {
    const texts: Record<string, string> = {
      active: t('memberStatus.active'),
      retired: t('memberStatus.retired'),
      expelled: t('memberStatus.expelled'),
      deceased: t('memberStatus.deceased')
    };
    return texts[status] || status;
  };

  const memberColumns = [
    {
      title: t('memberTable.name'),
      dataIndex: 'character_name',
      key: 'name',
      render: (name: string) => (
        <Space>
          <UserOutlined />
          <span>{name}</span>
        </Space>
      ),
      width: isMobile ? 100 : undefined,
    },
    {
      title: t('memberTable.position'),
      dataIndex: 'position',
      key: 'position',
      render: (position: string, record: OrganizationMember) => (
        <Tag color="blue">{position} {!isMobile && t('memberTable.rankSuffix', { rank: record.rank })}</Tag>
      ),
      width: isMobile ? 120 : undefined,
    },
    {
      title: t('memberTable.loyalty'),
      dataIndex: 'loyalty',
      key: 'loyalty',
      render: (loyalty: number) => (
        <span style={{ color: loyalty >= 70 ? 'green' : loyalty >= 40 ? 'orange' : 'red' }}>
          {loyalty}%
        </span>
      ),
      width: isMobile ? 80 : undefined,
    },
    {
      title: t('memberTable.contribution'),
      dataIndex: 'contribution',
      key: 'contribution',
      render: (contribution: number) => `${contribution}%`,
      width: isMobile ? 80 : undefined,
    },
    {
      title: t('memberTable.status'),
      dataIndex: 'status',
      key: 'status',
      render: (status: string) => (
        <Tag color={getStatusColor(status)}>{getStatusText(status)}</Tag>
      ),
      width: isMobile ? 80 : undefined,
    },
    {
      title: t('memberTable.joinedAt'),
      dataIndex: 'joined_at',
      key: 'joined_at',
      render: (time: string) => time || '-',
      width: isMobile ? 120 : undefined,
    },
    {
      title: t('memberTable.actions'),
      key: 'action',
      render: (_: unknown, record: OrganizationMember) => (
        <Space size={isMobile ? 0 : 'small'}>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEditMember(record)}
            style={isMobile ? { padding: '4px' } : undefined}
          >
            {isMobile ? '' : t('actions.edit')}
          </Button>
          <Button
            type="link"
            danger
            size="small"
            icon={<DeleteOutlined />}
            onClick={() => handleRemoveMember(record.id)}
            style={isMobile ? { padding: '4px' } : undefined}
          >
            {isMobile ? '' : t('actions.remove')}
          </Button>
        </Space>
      ),
      width: isMobile ? 50 : undefined,
      fixed: isMobile ? 'right' as const : undefined,
    },
  ];

  // 过滤掉已是成员的角色
  const availableCharacters = characters.filter(
    c => !c.is_organization && !members.some(m => m.character_id === c.id)
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {contextHolder}
      
      {/* 页面标题 - 仅桌面端显示 */}
      {!isMobile && (
        <div style={{
          padding: '16px 0',
          marginBottom: 16,
          borderBottom: `1px solid ${token.colorBorderSecondary}`
        }}>
          <h2 style={{ margin: 0, fontSize: 24 }}>
            <BankOutlined style={{ marginRight: 8 }} />
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
        {/* 左侧组织列表 - 桌面端 */}
        {!isMobile && (
        <Card
          title={t('orgList.titleWithCount', { n: organizations.length })}
          style={{ width: 300, height: '100%', overflow: 'hidden' }}
          styles={{ body: { padding: 0, height: 'calc(100% - 57px)', overflow: 'auto' } }}
          loading={loading}
        >
          {organizations.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px 20px', color: token.colorTextTertiary }}>
              {t('orgList.empty')}
            </div>
          ) : (
            <Space direction="vertical" style={{ width: '100%', padding: '12px' }}>
              {organizations.map(org => (
                <Card
                  key={org.id}
                  size="small"
                  hoverable
                  style={{
                    cursor: 'pointer',
                    border: selectedOrg?.id === org.id ? `2px solid ${token.colorPrimary}` : `1px solid ${token.colorBorder}`,
                    background: selectedOrg?.id === org.id ? token.colorPrimaryBg : 'transparent'
                  }}
                  onClick={() => handleSelectOrganization(org)}
                >
                  <Space direction="vertical" size="small" style={{ width: '100%' }}>
                    <strong style={{ fontSize: 14 }}>{org.name}</strong>
                    <Tag color="blue">{org.type}</Tag>
                    <div style={{ fontSize: '12px', color: token.colorTextSecondary }}>
                      {t('orgList.summary', { members: org.member_count, power: org.power_level })}
                    </div>
                  </Space>
                </Card>
              ))}
            </Space>
          )}
        </Card>
        )}

        {/* 移动端组织列表抽屉 */}
      {isMobile && (
        <Drawer
          title={t('orgList.title')}
          placement="left"
          onClose={() => setOrgListVisible(false)}
          open={orgListVisible}
          width="85%"
          styles={{ body: { padding: 0 } }}
        >
          {organizations.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px 20px', color: token.colorTextTertiary }}>
              {t('orgList.empty')}
            </div>
          ) : (
            <Space direction="vertical" style={{ width: '100%', padding: '12px' }}>
              {organizations.map(org => (
                <Card
                  key={org.id}
                  size="small"
                  hoverable
                  style={{
                    cursor: 'pointer',
                    border: selectedOrg?.id === org.id ? `2px solid ${token.colorPrimary}` : `1px solid ${token.colorBorder}`,
                    background: selectedOrg?.id === org.id ? token.colorPrimaryBg : 'transparent'
                  }}
                  onClick={() => {
                    handleSelectOrganization(org);
                    setOrgListVisible(false);
                  }}
                >
                  <Space direction="vertical" size="small" style={{ width: '100%' }}>
                    <strong style={{ fontSize: 14 }}>{org.name}</strong>
                    <Tag color="blue">{org.type}</Tag>
                    <div style={{ fontSize: '12px', color: token.colorTextSecondary }}>
                      {t('orgList.summary', { members: org.member_count, power: org.power_level })}
                    </div>
                  </Space>
                </Card>
              ))}
            </Space>
          )}
        </Drawer>
        )}

        {/* 右侧内容区域 */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
        {!selectedOrg ? (
          <Card style={{ height: '100%' }}>
            <div style={{ textAlign: 'center', padding: '100px 20px', color: token.colorTextTertiary }}>
              {isMobile && organizations.length > 0 && (
                <Button
                  type="primary"
                  icon={<UnorderedListOutlined />}
                  onClick={() => setOrgListVisible(true)}
                  style={{ marginBottom: 20 }}
                >
                  {t('orgList.select')}
                </Button>
              )}
              <div>{t('orgList.selectHint')}</div>
            </div>
          </Card>
        ) : (
          <>
            {/* 工具栏 - 移动端显示项目标题和组织列表按钮 */}
            {isMobile && (
              <Card size="small" style={{ marginBottom: 8 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <Space>
                    <BankOutlined />
                    <span style={{ fontSize: 14, fontWeight: 600 }}>
                      {t('page.title')}
                    </span>
                    <Tag color="blue">{currentProject?.title}</Tag>
                  </Space>
                  <Button
                    icon={<UnorderedListOutlined />}
                    onClick={() => setOrgListVisible(true)}
                    size="small"
                  >
                    {t('orgList.listButton')}
                  </Button>
                </div>
              </Card>
            )}

            {/* 内容区域 */}
            <div style={{
              flex: 1,
              display: 'flex',
              gap: isMobile ? 0 : 16,
              overflow: 'hidden'
            }}>
              <Card
                style={{ flex: 1, overflow: 'auto' }}
                styles={{ body: { padding: isMobile ? '12px' : '24px' } }}
              >
                <Space direction="vertical" style={{ width: '100%' }} size={isMobile ? 'middle' : 'large'}>
                <Card
                  title={t('orgDetail.title')}
                  size="small"
                  extra={
                    <Button
                      type="link"
                      size="small"
                      icon={<EditOutlined />}
                      onClick={() => {
                        editOrgForm.setFieldsValue({
                          power_level: selectedOrg.power_level,
                          location: selectedOrg.location,
                          motto: selectedOrg.motto,
                          color: selectedOrg.color
                        });
                        setIsEditOrgModalOpen(true);
                      }}
                    >
                      {t('actions.edit')}
                    </Button>
                  }
                >
                  <Descriptions column={isMobile ? 1 : 2} size="small">
                    <Descriptions.Item label={t('orgDetail.name')}>{selectedOrg.name}</Descriptions.Item>
                    <Descriptions.Item label={t('orgDetail.type')}>{selectedOrg.type}</Descriptions.Item>
                    <Descriptions.Item label={t('orgDetail.memberCount')}>{selectedOrg.member_count}</Descriptions.Item>
                    <Descriptions.Item label={t('orgDetail.powerLevel')}>
                      <Tag color={selectedOrg.power_level >= 70 ? 'red' : selectedOrg.power_level >= 50 ? 'orange' : 'default'}>
                        {selectedOrg.power_level}
                      </Tag>
                    </Descriptions.Item>
                    {selectedOrg.location && (
                      <Descriptions.Item label={t('orgDetail.location')} span={isMobile ? 1 : 2}>
                        {selectedOrg.location}
                      </Descriptions.Item>
                    )}
                    {selectedOrg.color && (
                      <Descriptions.Item label={t('orgDetail.color')}>
                        {selectedOrg.color}
                      </Descriptions.Item>
                    )}
                    {selectedOrg.motto && (
                      <Descriptions.Item label={t('orgDetail.motto')} span={2}>
                        {selectedOrg.motto}
                      </Descriptions.Item>
                    )}
                    <Descriptions.Item label={t('orgDetail.purpose')} span={2}>
                      {selectedOrg.purpose}
                    </Descriptions.Item>
                  </Descriptions>
                </Card>

                <Card
                  title={t('members.titleWithCount', { n: members.length })}
                  extra={
                    <Button
                      type="primary"
                      size="small"
                      icon={<PlusOutlined />}
                      onClick={() => setIsAddMemberModalOpen(true)}
                      disabled={availableCharacters.length === 0}
                    >
                      {t('members.add')}
                    </Button>
                  }
                >
                  <Table
                    columns={memberColumns}
                    dataSource={members}
                    rowKey="id"
                    pagination={
                      members.length > 5
                        ? {
                          defaultPageSize: 5,
                          showSizeChanger: true,
                          showQuickJumper: !isMobile,
                          showTotal: (total) => t('members.total', { n: total }),
                          pageSizeOptions: [5, 10, 20],
                          simple: isMobile,
                          position: ['bottomCenter'],
                        }
                        : false
                    }
                    size="small"
                    scroll={{
                      x: isMobile ? 'max-content' : undefined,
                      y: members.length > 10 ? 500 : undefined,
                    }}
                  />
                </Card>
                </Space>
              </Card>
            </div>
          </>
        )}
        </div>
      </div>

      {/* 添加成员模态框 */}
      <Modal
        title={t('addMember.title')}
        open={isAddMemberModalOpen}
        onCancel={() => {
          setIsAddMemberModalOpen(false);
          form.resetFields();
        }}
        footer={null}
        centered={!isMobile}
        width={isMobile ? '100%' : 500}
        style={isMobile ? { top: 0, paddingBottom: 0, maxWidth: '100vw' } : undefined}
        styles={isMobile ? { body: { maxHeight: 'calc(100vh - 110px)', overflowY: 'auto' } } : undefined}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleAddMember}
        >
          <Form.Item
            name="character_id"
            label={t('addMember.character')}
            rules={[{ required: true, message: t('addMember.characterRequired') }]}
          >
            <Select
              placeholder={t('addMember.characterPlaceholder')}
              showSearch
              filterOption={(input, option) =>
                (option?.label ?? '').toLowerCase().includes(input.toLowerCase())
              }
              options={availableCharacters.map(c => ({
                label: c.name,
                value: c.id
              }))}
            />
          </Form.Item>

          <Form.Item
            name="position"
            label={t('memberForm.position')}
            rules={[{ required: true, message: t('memberForm.positionRequired') }]}
          >
            <Input placeholder={t('memberForm.positionPlaceholder')} />
          </Form.Item>

          <Form.Item
            name="rank"
            label={t('memberForm.rank')}
            initialValue={5}
            tooltip={t('memberForm.rankTooltip')}
          >
            <InputNumber min={0} max={10} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="loyalty"
            label={t('addMember.initialLoyalty')}
            initialValue={50}
          >
            <InputNumber min={0} max={100} style={{ width: '100%' }} addonAfter="%" />
          </Form.Item>

          <Form.Item
            name="status"
            label={t('memberForm.status')}
            initialValue="active"
          >
            <Select>
              <Select.Option value="active">{t('memberStatus.active')}</Select.Option>
              <Select.Option value="retired">{t('memberStatus.retired')}</Select.Option>
              <Select.Option value="expelled">{t('memberStatus.expelled')}</Select.Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="joined_at"
            label={t('memberForm.joinedAt')}
          >
            <Input placeholder={t('memberForm.joinedAtPlaceholder')} />
          </Form.Item>

          <Form.Item>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => setIsAddMemberModalOpen(false)}>{t('buttons.cancel')}</Button>
              <Button type="primary" htmlType="submit">
                {t('buttons.add')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑成员模态框 */}
      <Modal
        title={t('editMember.title')}
        open={isEditMemberModalOpen}
        onCancel={() => {
          setIsEditMemberModalOpen(false);
          editMemberForm.resetFields();
          setEditingMember(null);
        }}
        footer={null}
        centered={true}
        width={isMobile ? '90%' : 500}
        style={isMobile ? {
          maxWidth: '90vw',
          margin: '0 auto'
        } : undefined}
        styles={isMobile ? {
          body: {
            maxHeight: 'calc(80vh - 110px)',
            overflowY: 'auto',
            padding: '20px 16px'
          }
        } : undefined}
      >
        <Form
          form={editMemberForm}
          layout="vertical"
          onFinish={handleUpdateMember}
        >
          <Form.Item
            name="position"
            label={t('memberForm.position')}
            rules={[{ required: true, message: t('memberForm.positionRequired') }]}
          >
            <Input placeholder={t('memberForm.positionPlaceholder')} />
          </Form.Item>

          <Form.Item
            name="rank"
            label={t('memberForm.rank')}
            tooltip={t('memberForm.rankTooltip')}
          >
            <InputNumber min={0} max={10} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="loyalty"
            label={t('memberTable.loyalty')}
          >
            <InputNumber min={0} max={100} style={{ width: '100%' }} addonAfter="%" />
          </Form.Item>

          <Form.Item
            name="contribution"
            label={t('memberTable.contribution')}
          >
            <InputNumber min={0} max={100} style={{ width: '100%' }} addonAfter="%" />
          </Form.Item>

          <Form.Item
            name="status"
            label={t('memberForm.status')}
          >
            <Select>
              <Select.Option value="active">{t('memberStatus.active')}</Select.Option>
              <Select.Option value="retired">{t('memberStatus.retired')}</Select.Option>
              <Select.Option value="expelled">{t('memberStatus.expelled')}</Select.Option>
              <Select.Option value="deceased">{t('memberStatus.deceased')}</Select.Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="joined_at"
            label={t('memberForm.joinedAt')}
          >
            <Input placeholder={t('memberForm.joinedAtPlaceholder')} />
          </Form.Item>

          <Form.Item
            name="notes"
            label={t('editMember.notes')}
          >
            <Input.TextArea rows={3} placeholder={t('editMember.notesPlaceholder')} />
          </Form.Item>

          <Form.Item>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setIsEditMemberModalOpen(false);
                editMemberForm.resetFields();
                setEditingMember(null);
              }}>
                {t('buttons.cancel')}
              </Button>
              <Button type="primary" htmlType="submit">
                {t('buttons.save')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑组织模态框 */}
      <Modal
        title={t('editOrg.title')}
        open={isEditOrgModalOpen}
        onCancel={() => {
          setIsEditOrgModalOpen(false);
          editOrgForm.resetFields();
        }}
        footer={null}
        centered={!isMobile}
        width={isMobile ? '100%' : 500}
        style={isMobile ? { top: 0, paddingBottom: 0, maxWidth: '100vw' } : undefined}
        styles={isMobile ? { body: { maxHeight: 'calc(100vh - 110px)', overflowY: 'auto' } } : undefined}
      >
        <Form
          form={editOrgForm}
          layout="vertical"
          onFinish={async (values) => {
            if (!selectedOrg) return;
            try {
              await axios.put(`/api/organizations/${selectedOrg.id}`, values);
              message.success(t('toast.orgUpdated'));
              setIsEditOrgModalOpen(false);
              editOrgForm.resetFields();

              // 重新获取更新后的组织列表
              const res = await axios.get(`/api/organizations/project/${projectId}`);
              setOrganizations(res.data);

              // 更新当前选中的组织详情
              const updatedOrg = res.data.find((org: Organization) => org.id === selectedOrg.id);
              if (updatedOrg) {
                setSelectedOrg(updatedOrg);
              }

              // 刷新全局 store
              await refreshCharacters();
            } catch (error) {
              message.error(t('toast.updateFailed'));
              console.error(error);
            }
          }}
        >
          <Form.Item
            name="power_level"
            label={t('orgDetail.powerLevel')}
            rules={[{ required: true, message: t('editOrg.powerRequired') }]}
            tooltip={t('editOrg.powerTooltip')}
          >
            <InputNumber min={0} max={100} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="location"
            label={t('orgDetail.location')}
          >
            <Input placeholder={t('editOrg.locationPlaceholder')} />
          </Form.Item>

          <Form.Item
            name="motto"
            label={t('orgDetail.motto')}
          >
            <Input placeholder={t('editOrg.mottoPlaceholder')} />
          </Form.Item>

          <Form.Item
            name="color"
            label={t('orgDetail.color')}
          >
            <Input placeholder={t('editOrg.colorPlaceholder')} />
          </Form.Item>

          <Form.Item>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => setIsEditOrgModalOpen(false)}>{t('buttons.cancel')}</Button>
              <Button type="primary" htmlType="submit">
                {t('buttons.save')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
