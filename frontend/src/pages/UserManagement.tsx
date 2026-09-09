import { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  Switch,
  Space,
  Tag,
  Popconfirm,
  message,
  Card,
  Typography,
  Badge,
  InputNumber,
  Row,
  Col,
  Pagination,
  Dropdown,
  theme,
} from 'antd';
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  KeyOutlined,
  StopOutlined,
  CheckCircleOutlined,
  ArrowLeftOutlined,
  TeamOutlined,
  UserOutlined,
  SearchOutlined,
  MoreOutlined,
} from '@ant-design/icons';
import { adminApi } from '../services/api';
import type { User } from '../types';
import UserMenu from '../components/UserMenu';
import { useTranslation } from 'react-i18next';
import i18n from '../i18n';

const { Title, Text } = Typography;

interface UserWithStatus extends User {
  is_active?: boolean;
}

type SortField =
  | 'username'
  | 'display_name'
  | 'is_active'
  | 'is_admin'
  | 'trust_level'
  | 'created_at'
  | 'last_login';

type SortOrder = 'ascend' | 'descend' | null;

export default function UserManagement() {
  const { t } = useTranslation('userManagement');
  const navigate = useNavigate();
  const [users, setUsers] = useState<UserWithStatus[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalVisible, setModalVisible] = useState(false);
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [resetPasswordModalVisible, setResetPasswordModalVisible] = useState(false);
  const [currentUser, setCurrentUser] = useState<UserWithStatus | null>(null);
  const [newPassword, setNewPassword] = useState('');
  const [pageSize, setPageSize] = useState(20);
  const [currentPage, setCurrentPage] = useState(1);
  const [searchText, setSearchText] = useState('');
  const [sortField, setSortField] = useState<SortField | null>('created_at');
  const [sortOrder, setSortOrder] = useState<SortOrder>('descend');

  const [form] = Form.useForm();
  const [editForm] = Form.useForm();
  const [modal, contextHolder] = Modal.useModal();
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) => `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;

  // 过滤用户列表
  const filteredUsers = users.filter(user => {
    if (!searchText) return true;
    const searchLower = searchText.toLowerCase();
    return (
      user.username?.toLowerCase().includes(searchLower) ||
      user.display_name?.toLowerCase().includes(searchLower) ||
      user.user_id?.toLowerCase().includes(searchLower)
    );
  });

  // 排序后的用户列表
  const sortedUsers = useMemo(() => {
    if (!sortField || !sortOrder) {
      return filteredUsers;
    }

    const compareValues = (
      a: string | number | boolean | null | undefined,
      b: string | number | boolean | null | undefined
    ) => {
      // 空值始终置底
      if (a == null && b == null) return 0;
      if (a == null) return 1;
      if (b == null) return -1;

      if (typeof a === 'string' && typeof b === 'string') {
        return a.localeCompare(b, i18n.language?.startsWith('zh') ? 'zh-CN' : 'en');
      }

      if (typeof a === 'boolean' && typeof b === 'boolean') {
        return Number(a) - Number(b);
      }

      return Number(a) - Number(b);
    };

    const getSortValue = (user: UserWithStatus) => {
      switch (sortField) {
        case 'username':
          return user.username ?? null;
        case 'display_name':
          return user.display_name ?? null;
        case 'is_active':
          return user.is_active !== false;
        case 'is_admin':
          return user.is_admin;
        case 'trust_level':
          return user.trust_level ?? null;
        case 'created_at':
          return user.created_at ? new Date(user.created_at).getTime() : null;
        case 'last_login':
          return user.last_login ? new Date(user.last_login).getTime() : null;
        default:
          return null;
      }
    };

    const sorted = [...filteredUsers].sort((a, b) => {
      const result = compareValues(getSortValue(a), getSortValue(b));
      return sortOrder === 'ascend' ? result : -result;
    });

    return sorted;
  }, [filteredUsers, sortField, sortOrder]);

  // 加载用户列表
  const loadUsers = async () => {
    setLoading(true);
    try {
      const res = await adminApi.getUsers();
      setUsers(res.users);
    } catch (error) {
      console.error('加载用户列表失败:', error);
      message.error(t('toast.loadFailed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadUsers();
  }, []);

  // 添加用户
  interface CreateUserValues {
    username: string;
    display_name: string;
    password?: string;
    avatar_url?: string;
    trust_level?: number;
    is_admin?: boolean;
  }

  const handleCreate = async (values: CreateUserValues) => {
    try {
      const res = await adminApi.createUser(values);
      message.success(t('toast.createSuccess'));

      // 如果有默认密码，显示给管理员
      if (res.default_password) {
        modal.info({
          title: t('createResult.title'),
          content: (
            <div>
              <p>{t('createResult.username')}<Text strong>{values.username}</Text></p>
              <p>{t('createResult.initialPassword')}<Text strong copyable>{res.default_password}</Text></p>
              <p style={{ color: token.colorError, marginTop: 16 }}>
                {t('createResult.warning')}
              </p>
            </div>
          ),
          width: 500,
          centered: true,
        });
      }

      setModalVisible(false);
      form.resetFields();
      loadUsers();
    } catch (error) {
      console.error('创建用户失败:', error);
      message.error(t('toast.createFailed'));
    }
  };

  // 编辑用户
  const handleEdit = (user: UserWithStatus) => {
    setCurrentUser(user);
    editForm.setFieldsValue({
      display_name: user.display_name,
      avatar_url: user.avatar_url,
      trust_level: user.trust_level,
      is_admin: user.is_admin,
    });
    setEditModalVisible(true);
  };

  interface UpdateUserValues {
    display_name: string;
    avatar_url?: string;
    trust_level?: number;
    is_admin?: boolean;
  }

  const handleUpdate = async (values: UpdateUserValues) => {
    if (!currentUser) return;

    try {
      await adminApi.updateUser(currentUser.user_id, values);
      message.success(t('toast.updateSuccess'));
      setEditModalVisible(false);
      editForm.resetFields();
      loadUsers();
    } catch (error) {
      console.error('更新用户失败:', error);
      message.error(t('toast.updateFailed'));
    }
  };

  // 切换用户状态
  const handleToggleStatus = async (user: UserWithStatus) => {
    const isActive = user.is_active !== false;

    try {
      await adminApi.toggleUserStatus(user.user_id, !isActive);
      message.success(isActive ? t('toast.userDisabled') : t('toast.userEnabled'));
      loadUsers();
    } catch (error) {
      console.error('切换用户状态失败:', error);
      message.error(isActive ? t('toast.disableFailed') : t('toast.enableFailed'));
    }
  };

  // 重置密码
  const handleResetPassword = (user: UserWithStatus) => {
    setCurrentUser(user);
    setNewPassword('');
    setResetPasswordModalVisible(true);
  };

  const handleResetPasswordConfirm = async () => {
    if (!currentUser) return;

    try {
      const res = await adminApi.resetPassword(
        currentUser.user_id,
        newPassword || undefined
      );

      modal.info({
        title: t('resetResult.title'),
        content: (
          <div>
            <p>{t('resetResult.user')}<Text strong>{currentUser.username}</Text></p>
            <p>{t('resetResult.newPassword')}<Text strong copyable>{res.new_password}</Text></p>
            <p style={{ color: token.colorError, marginTop: 16 }}>
              {t('resetResult.warning')}
            </p>
          </div>
        ),
        width: 500,
        centered: true,
      });

      setResetPasswordModalVisible(false);
      setNewPassword('');
    } catch (error) {
      console.error('重置密码失败:', error);
      message.error(t('toast.resetFailed'));
    }
  };

  // 删除用户
  const handleDelete = async (user: UserWithStatus) => {
    try {
      await adminApi.deleteUser(user.user_id);
      message.success(t('toast.deleteSuccess'));
      loadUsers();
    } catch (error) {
      console.error('删除用户失败:', error);
      message.error(t('toast.deleteFailed'));
    }
  };

  const isMobile = window.innerWidth <= 768;

  // 表格列定义
  const columns = [
    {
      title: t('table.username'),
      dataIndex: 'username',
      key: 'username',
      width: 150,
      sorter: true,
      sortOrder: sortField === 'username' ? sortOrder : null,
      render: (text: string) => (
        <Space>
          <UserOutlined style={{ color: token.colorPrimary }} />
          <Text strong>{text}</Text>
        </Space>
      ),
    },
    {
      title: t('table.displayName'),
      dataIndex: 'display_name',
      key: 'display_name',
      width: 150,
      sorter: true,
      sortOrder: sortField === 'display_name' ? sortOrder : null,
    },
    {
      title: t('table.status'),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 100,
      sorter: true,
      sortOrder: sortField === 'is_active' ? sortOrder : null,
      render: (isActive: boolean) => (
        <Badge
          status={isActive !== false ? 'success' : 'error'}
          text={isActive !== false ? t('status.active') : t('status.disabled')}
        />
      ),
    },
    {
      title: t('table.role'),
      dataIndex: 'is_admin',
      key: 'is_admin',
      width: 100,
      sorter: true,
      sortOrder: sortField === 'is_admin' ? sortOrder : null,
      render: (isAdmin: boolean) => (
        <Tag color={isAdmin ? 'gold' : 'blue'}>
          {isAdmin ? t('role.admin') : t('role.user')}
        </Tag>
      ),
    },
    {
      title: t('table.trustLevel'),
      dataIndex: 'trust_level',
      key: 'trust_level',
      width: 100,
      sorter: true,
      sortOrder: sortField === 'trust_level' ? sortOrder : null,
      render: (level: number) => (
        <Tag color={level === -1 ? 'default' : level >= 5 ? 'green' : 'blue'}>
          {level === -1 ? t('trust.disabled') : `Level ${level}`}
        </Tag>
      ),
    },
    {
      title: t('table.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      sorter: true,
      sortOrder: sortField === 'created_at' ? sortOrder : null,
      render: (date: string) => date ? new Date(date).toLocaleString(i18n.language?.startsWith('zh') ? 'zh-CN' : 'en-US') : '-',
    },
    {
      title: t('table.lastLogin'),
      dataIndex: 'last_login',
      key: 'last_login',
      width: 180,
      sorter: true,
      sortOrder: sortField === 'last_login' ? sortOrder : null,
      render: (date: string) => date ? new Date(date).toLocaleString(i18n.language?.startsWith('zh') ? 'zh-CN' : 'en-US') : t('neverLoggedIn'),
    },
    {
      title: t('table.actions'),
      key: 'action',
      width: isMobile ? 80 : 300,
      fixed: 'right' as const,
      render: (_: unknown, record: UserWithStatus) => {
        const isActive = record.is_active !== false;

        // 移动端：使用下拉菜单
        if (isMobile) {
          const menuItems = [
            {
              key: 'edit',
              label: t('actions.editUser'),
              icon: <EditOutlined />,
              onClick: () => handleEdit(record),
            },
            {
              key: 'reset',
              label: t('actions.resetPassword'),
              icon: <KeyOutlined />,
              onClick: () => handleResetPassword(record),
            },
            {
              key: 'toggle',
              label: isActive ? t('actions.disableUser') : t('actions.enableUser'),
              icon: isActive ? <StopOutlined /> : <CheckCircleOutlined />,
              danger: isActive,
              onClick: () => {
                modal.confirm({
                  title: isActive ? t('confirm.disableUser') : t('confirm.enableUser'),
                  onOk: () => handleToggleStatus(record),
                  okText: t('confirm.ok'),
                  cancelText: t('confirm.cancel'),
                });
              },
            },
            ...(!record.is_admin ? [{
              key: 'delete',
              label: t('actions.deleteUser'),
              icon: <DeleteOutlined />,
              danger: true,
              onClick: () => {
                modal.confirm({
                  title: t('confirm.deleteUser'),
                  onOk: () => handleDelete(record),
                  okText: t('confirm.ok'),
                  cancelText: t('confirm.cancel'),
                  okButtonProps: { danger: true },
                });
              },
            }] : []),
          ];

          return (
            <Dropdown menu={{ items: menuItems }} trigger={['click']}>
              <Button type="text" icon={<MoreOutlined />} />
            </Dropdown>
          );
        }

        // 桌面端：保持原有按钮样式
        return (
          <Space size="small">
            <Button
              type="link"
              size="small"
              icon={<EditOutlined />}
              onClick={() => handleEdit(record)}
            >
              {t('actions.edit')}
            </Button>

            <Button
              type="link"
              size="small"
              icon={<KeyOutlined />}
              onClick={() => handleResetPassword(record)}
            >
              {t('actions.resetPassword')}
            </Button>

            <Popconfirm
              title={isActive ? t('confirm.disableUser') : t('confirm.enableUser')}
              onConfirm={() => handleToggleStatus(record)}
              okText={t('confirm.ok')}
              cancelText={t('confirm.cancel')}
            >
              <Button
                type="link"
                size="small"
                danger={isActive}
                icon={isActive ? <StopOutlined /> : <CheckCircleOutlined />}
              >
                {isActive ? t('actions.disable') : t('actions.enable')}
              </Button>
            </Popconfirm>

            {!record.is_admin && (
              <Popconfirm
                title={t('confirm.deleteUser')}
                onConfirm={() => handleDelete(record)}
                okText={t('confirm.ok')}
                cancelText={t('confirm.cancel')}
                okButtonProps={{ danger: true }}
              >
                <Button
                  type="link"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                >
                  {t('actions.delete')}
                </Button>
              </Popconfirm>
            )}
          </Space>
        );
      },
    },
  ];

  return (
    <div style={{
      height: '100vh',
      background: `linear-gradient(180deg, ${token.colorBgLayout} 0%, ${alphaColor(token.colorPrimary, 0.08)} 100%)`,
      padding: isMobile ? '20px 16px' : '40px 24px',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
    }}>
      {contextHolder}
      <div style={{
        maxWidth: 1400,
        margin: '0 auto',
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}>
        {/* 顶部导航卡片 */}
        <Card
          variant="borderless"
          style={{
            background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.8)} 50%, ${token.colorPrimaryHover} 100%)`,
            borderRadius: isMobile ? 16 : 24,
            boxShadow: `0 12px 40px ${alphaColor(token.colorPrimary, 0.25)}, 0 4px 12px ${alphaColor(token.colorText, 0.08)}`,
            marginBottom: isMobile ? 20 : 24,
            border: 'none',
            position: 'relative',
            overflow: 'hidden'
          }}
        >
          {/* 装饰性背景元素 */}
          <div style={{ position: 'absolute', top: -60, right: -60, width: 200, height: 200, borderRadius: '50%', background: alphaColor(token.colorWhite, 0.08), pointerEvents: 'none' }} />
          <div style={{ position: 'absolute', bottom: -40, left: '30%', width: 120, height: 120, borderRadius: '50%', background: alphaColor(token.colorWhite, 0.05), pointerEvents: 'none' }} />
          <div style={{ position: 'absolute', top: '50%', right: '15%', width: 80, height: 80, borderRadius: '50%', background: alphaColor(token.colorWhite, 0.06), pointerEvents: 'none' }} />

          <Row align="middle" justify="space-between" gutter={[16, 16]} style={{ position: 'relative', zIndex: 1 }}>
            <Col xs={24} sm={12}>
              <Space direction="vertical" size={4}>
                <Title level={isMobile ? 3 : 2} style={{ margin: 0, color: token.colorWhite, textShadow: `0 2px 4px ${alphaColor(token.colorText, 0.2)}` }}>
                  <TeamOutlined style={{ color: alphaColor(token.colorWhite, 0.9), marginRight: 12 }} />
                  {t('page.title')}
                </Title>
                <Text style={{ fontSize: isMobile ? 12 : 14, color: alphaColor(token.colorWhite, 0.85) }}>
                  {t('page.subtitle')}
                </Text>
              </Space>
            </Col>
            <Col xs={24} sm={12}>
              <Space size={12} style={{ display: 'flex', justifyContent: isMobile ? 'flex-start' : 'flex-end', width: '100%' }}>
                <Button
                  icon={<ArrowLeftOutlined />}
                  onClick={() => navigate('/')}
                  style={{
                    borderRadius: 12,
                    background: alphaColor(token.colorWhite, 0.15),
                    border: `1px solid ${alphaColor(token.colorWhite, 0.3)}`,
                    boxShadow: `0 2px 8px ${alphaColor(token.colorText, 0.15)}`,
                    color: token.colorWhite,
                    backdropFilter: 'blur(10px)',
                    transition: 'all 0.3s ease'
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = alphaColor(token.colorWhite, 0.25);
                    e.currentTarget.style.transform = 'translateY(-1px)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = alphaColor(token.colorWhite, 0.15);
                    e.currentTarget.style.transform = 'none';
                  }}
                >
                  {t('page.backHome')}
                </Button>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setModalVisible(true)}
                  style={{
                    borderRadius: 12,
                    background: alphaColor(token.colorWarning, 0.95),
                    border: `1px solid ${alphaColor(token.colorWhite, 0.3)}`,
                    boxShadow: `0 4px 16px ${alphaColor(token.colorWarning, 0.4)}`,
                    color: token.colorWhite,
                    fontWeight: 600
                  }}
                >
                  {t('page.addUser')}
                </Button>
                <UserMenu />
              </Space>
            </Col>
          </Row>
        </Card>

        {/* 主内容卡片 */}
        <Card
          variant="borderless"
          style={{
            background: alphaColor(token.colorBgContainer, 0.72),
            borderRadius: isMobile ? 16 : 24,
            border: `1px solid ${alphaColor(token.colorWhite, 0.45)}`,
            backdropFilter: 'blur(20px)',
            boxShadow: `0 4px 24px ${alphaColor(token.colorText, 0.06)}`,
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
          styles={{
            body: {
              padding: 0,
              height: '100%',
              display: 'flex',
              flexDirection: 'column',
              overflow: 'hidden',
            },
          }}
        >
          {/* 搜索栏 */}
          <div style={{
            padding: '16px 24px 0 24px',
            borderBottom: `1px solid ${alphaColor(token.colorText, 0.06)}`,
          }}>
            <Input
              placeholder={t('search.placeholder')}
              prefix={<SearchOutlined style={{ color: token.colorTextTertiary }} />}
              value={searchText}
              onChange={(e) => {
                setSearchText(e.target.value);
                setCurrentPage(1); // 搜索时重置到第一页
              }}
              allowClear
              style={{
                borderRadius: 8,
              }}
            />
          </div>

          {/* 表格区域 */}
          <div style={{
            flex: 1,
            overflow: 'auto',
            padding: '16px 24px 0 24px',
          }}>
            <Table
              columns={columns}
              dataSource={sortedUsers.slice((currentPage - 1) * pageSize, currentPage * pageSize)}
              rowKey="user_id"
              loading={loading}
              scroll={{
                x: 1400,
                y: 'calc(100vh - 410px)'
              }}
              pagination={false}
              onChange={(_pagination, _filters, sorter) => {
                const currentSorter = Array.isArray(sorter) ? sorter[0] : sorter;
                setCurrentPage(1);

                if (currentSorter && currentSorter.field && currentSorter.order) {
                  setSortField(currentSorter.field as SortField);
                  setSortOrder(currentSorter.order as SortOrder);
                } else {
                  setSortField(null);
                  setSortOrder(null);
                }
              }}
            />
          </div>

          {/* 固定分页控件 */}
          <div style={{
            padding: '16px 24px 24px 24px',
            borderTop: `1px solid ${alphaColor(token.colorText, 0.06)}`,
            background: 'transparent',
            display: 'flex',
            justifyContent: 'center',
          }}>
            <Pagination
              current={currentPage}
              pageSize={pageSize}
              total={filteredUsers.length}
              showSizeChanger
              showTotal={(total) => `${t('pagination.total', { n: total })}${searchText ? t('pagination.filtered') : ''}`}
              pageSizeOptions={[20, 50, 100]}
              onChange={(page, size) => {
                setCurrentPage(page);
                setPageSize(size);
              }}
              onShowSizeChange={(_current, size) => {
                setCurrentPage(1);
                setPageSize(size);
              }}
            />
          </div>
        </Card>
      </div>

      {/* 添加用户对话框 */}
      <Modal
        title={<span><PlusOutlined style={{ marginRight: 8 }} />{t('createModal.title')}</span>}
        open={modalVisible}
        onCancel={() => {
          setModalVisible(false);
          form.resetFields();
        }}
        onOk={() => form.submit()}
        width={isMobile ? '90%' : 600}
        centered
        okText={t('createModal.ok')}
        cancelText={t('createModal.cancel')}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleCreate}
        >
          <Form.Item
            label={t('form.username')}
            name="username"
            rules={[
              { required: true, message: t('form.usernameRequired') },
              { min: 3, max: 20, message: t('form.usernameLength') },
              { pattern: /^[a-zA-Z0-9_]+$/, message: t('form.usernamePattern') },
            ]}
          >
            <Input placeholder={t('form.usernamePlaceholder')} />
          </Form.Item>

          <Form.Item
            label={t('form.displayName')}
            name="display_name"
            rules={[
              { required: true, message: t('form.displayNameRequired') },
              { min: 2, max: 50, message: t('form.displayNameLength') },
            ]}
          >
            <Input placeholder={t('form.displayNamePlaceholder')} />
          </Form.Item>

          <Form.Item
            label={t('form.initialPassword')}
            name="password"
            extra={t('form.initialPasswordExtra')}
            rules={[
              { min: 6, message: t('form.passwordMin') },
            ]}
          >
            <Input.Password placeholder={t('form.initialPasswordPlaceholder')} />
          </Form.Item>

          <Form.Item
            label={t('form.avatarUrl')}
            name="avatar_url"
          >
            <Input placeholder={t('form.avatarUrlPlaceholder')} />
          </Form.Item>

          <Form.Item
            label={t('form.trustLevel')}
            name="trust_level"
            initialValue={0}
          >
            <InputNumber min={0} max={9} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            label={t('form.setAdmin')}
            name="is_admin"
            valuePropName="checked"
            initialValue={false}
          >
            <Switch
              size={isMobile ? 'small' : 'default'}
              style={{
                flexShrink: 0,
                height: isMobile ? 16 : 22,
                minHeight: isMobile ? 16 : 22,
                lineHeight: isMobile ? '16px' : '22px'
              }}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑用户对话框 */}
      <Modal
        title={<span><EditOutlined style={{ marginRight: 8 }} />{t('editModal.title')}</span>}
        open={editModalVisible}
        onCancel={() => {
          setEditModalVisible(false);
          editForm.resetFields();
        }}
        onOk={() => editForm.submit()}
        width={isMobile ? '90%' : 600}
        centered
        okText={t('editModal.ok')}
        cancelText={t('editModal.cancel')}
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={handleUpdate}
        >
          <Form.Item
            label={t('form.displayName')}
            name="display_name"
            rules={[
              { required: true, message: t('form.displayNameRequired') },
              { min: 2, max: 50, message: t('form.displayNameLength') },
            ]}
          >
            <Input placeholder={t('form.displayNamePlaceholder')} />
          </Form.Item>

          <Form.Item
            label={t('form.avatarUrl')}
            name="avatar_url"
          >
            <Input placeholder={t('form.avatarUrlPlaceholder')} />
          </Form.Item>

          <Form.Item
            label={t('form.trustLevel')}
            name="trust_level"
          >
            <InputNumber min={0} max={9} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            label={t('form.setAdmin')}
            name="is_admin"
            valuePropName="checked"
          >
            <Switch
              size={isMobile ? 'small' : 'default'}
              style={{
                flexShrink: 0,
                height: isMobile ? 16 : 22,
                minHeight: isMobile ? 16 : 22,
                lineHeight: isMobile ? '16px' : '22px'
              }}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* 重置密码对话框 */}
      <Modal
        title={<span><KeyOutlined style={{ marginRight: 8 }} />{t('resetModal.title')}</span>}
        open={resetPasswordModalVisible}
        onCancel={() => {
          setResetPasswordModalVisible(false);
          setNewPassword('');
        }}
        onOk={handleResetPasswordConfirm}
        width={isMobile ? '90%' : 500}
        centered
        okText={t('resetModal.ok')}
        cancelText={t('resetModal.cancel')}
      >
        <div style={{ marginBottom: 16 }}>
          <Text>{t('resetModal.user')}<Text strong>{currentUser?.username}</Text></Text>
        </div>
        <Form layout="vertical">
          <Form.Item
            label={t('resetModal.newPassword')}
            extra={t('resetModal.extra')}
          >
            <Input.Password
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder={t('resetModal.placeholder')}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}