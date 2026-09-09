import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Card, Table, Button, Tag, Space, Modal, Form, Input, Select,
  InputNumber, Switch, message, Tooltip, Popconfirm, Statistic,
  Row, Col, Empty, Divider, Badge, Alert, Pagination, Dropdown, theme
} from 'antd';
import type { MenuProps } from 'antd';
import {
  PlusOutlined, SyncOutlined, EditOutlined, DeleteOutlined,
  CheckCircleOutlined, CloseCircleOutlined, ExclamationCircleOutlined,
  BulbOutlined, EyeOutlined, FlagOutlined, WarningOutlined,
  ClockCircleOutlined, MoreOutlined, ReloadOutlined, InfoCircleOutlined
} from '@ant-design/icons';
import { foreshadowApi, chapterApi, characterApi } from '../services/api';
import type {
  Foreshadow, ForeshadowCreate, ForeshadowUpdate, ForeshadowStats,
  ForeshadowStatus, ForeshadowCategory, Chapter, Character
} from '../types';
import { eventBus, EventNames } from '../store/eventBus';

const { TextArea } = Input;
const { Option } = Select;

export default function Foreshadows() {
  const { t, i18n } = useTranslation('foreshadows');
  const sortLocale = i18n.language?.startsWith('en') ? 'en' : 'zh-CN';
  const { projectId } = useParams<{ projectId: string }>();
  const [loading, setLoading] = useState(false);
  const [foreshadows, setForeshadows] = useState<Foreshadow[]>([]);
  const [stats, setStats] = useState<ForeshadowStats | null>(null);
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [total, setTotal] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  
  // 筛选条件
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [categoryFilter, setCategoryFilter] = useState<string | undefined>(undefined);
  const [sourceFilter, setSourceFilter] = useState<string | undefined>(undefined);
  
  // 模态框状态
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [syncModalVisible, setSyncModalVisible] = useState(false);
  const [detailModalVisible, setDetailModalVisible] = useState(false);
  const [plantModalVisible, setPlantModalVisible] = useState(false);
  const [resolveModalVisible, setResolveModalVisible] = useState(false);
  
  const [currentForeshadow, setCurrentForeshadow] = useState<Foreshadow | null>(null);
  const [form] = Form.useForm();
  const [plantForm] = Form.useForm();
  const [resolveForm] = Form.useForm();
  const [syncing, setSyncing] = useState(false);
  
  // 表格容器引用，用于计算滚动高度
  const tableContainerRef = useRef<HTMLDivElement>(null);
  const [tableScrollY, setTableScrollY] = useState<number>(400);
  const { token } = theme.useToken();

  const STATUS_CONFIG: Record<ForeshadowStatus, { label: string; color: string; icon: React.ReactNode }> = {
    pending: { label: t('status.pending'), color: 'default', icon: <ClockCircleOutlined /> },
    planted: { label: t('status.planted'), color: 'green', icon: <BulbOutlined /> },
    resolved: { label: t('status.resolved'), color: 'blue', icon: <CheckCircleOutlined /> },
    partially_resolved: { label: t('status.partiallyResolved'), color: 'orange', icon: <ExclamationCircleOutlined /> },
    abandoned: { label: t('status.abandoned'), color: 'default', icon: <CloseCircleOutlined /> },
  };

  const CATEGORY_CONFIG: Record<string, { label: string; color: string }> = {
    identity: { label: t('category.identity'), color: 'purple' },
    mystery: { label: t('category.mystery'), color: 'magenta' },
    item: { label: t('category.item'), color: 'gold' },
    relationship: { label: t('category.relationship'), color: 'cyan' },
    event: { label: t('category.event'), color: 'blue' },
    ability: { label: t('category.ability'), color: 'green' },
    prophecy: { label: t('category.prophecy'), color: 'volcano' },
  };

  // 加载伏笔列表
  const loadForeshadows = useCallback(async () => {
    if (!projectId) return;
    
    setLoading(true);
    try {
      const response = await foreshadowApi.getProjectForeshadows(projectId, {
        status: statusFilter,
        category: categoryFilter,
        source_type: sourceFilter,
        page: currentPage,
        limit: pageSize,
      });
      
      setForeshadows(response.items);
      setTotal(response.total);
      if (response.stats) {
        setStats(response.stats);
      }
    } catch (error) {
      console.error('加载伏笔列表失败:', error);
    } finally {
      setLoading(false);
    }
  }, [projectId, statusFilter, categoryFilter, sourceFilter, currentPage, pageSize]);

  // 加载章节列表（用于选择）
  const loadChapters = useCallback(async () => {
    if (!projectId) return;
    try {
      const chaptersData = await chapterApi.getChapters(projectId);
      setChapters(chaptersData);
    } catch (error) {
      console.error('加载章节列表失败:', error);
    }
  }, [projectId]);

  // 加载角色列表（用于关联角色）
  const loadCharacters = useCallback(async () => {
    if (!projectId) return;
    try {
      const charactersData = await characterApi.getCharacters(projectId);
      setCharacters(charactersData);
    } catch (error) {
      console.error('加载角色列表失败:', error);
    }
  }, [projectId]);

  // 加载统计
  const loadStats = useCallback(async () => {
    if (!projectId) return;
    try {
      // 获取当前最大章节号（只计算有内容的章节，与表格显示逻辑保持一致）
      const chaptersWithContent = chapters.filter(c => c.content);
      const maxChapter = chaptersWithContent.length > 0
        ? Math.max(...chaptersWithContent.map(c => c.chapter_number))
        : undefined;
      const statsData = await foreshadowApi.getForeshadowStats(projectId, maxChapter);
      setStats(statsData);
    } catch (error) {
      console.error('加载统计失败:', error);
    }
  }, [projectId, chapters]);

  useEffect(() => {
    loadForeshadows();
    loadChapters();
    loadCharacters();
  }, [loadForeshadows, loadChapters, loadCharacters]);

  useEffect(() => {
    const handleTaskSettled = (payload?: unknown) => {
      if (!payload || typeof payload !== 'object') return;
      const data = payload as { projectId?: string; resources?: string[] };
      if (data.projectId && data.projectId !== projectId) return;
      if (!data.resources?.includes('foreshadows')) return;
      void loadForeshadows();
      void loadStats();
    };
    eventBus.on(EventNames.BACKGROUND_TASK_SETTLED, handleTaskSettled);
    return () => eventBus.off(EventNames.BACKGROUND_TASK_SETTLED, handleTaskSettled);
  }, [loadForeshadows, loadStats, projectId]);

  // 计算表格滚动高度
  useEffect(() => {
    const calculateTableHeight = () => {
      if (tableContainerRef.current) {
        // 获取容器高度，减去表头高度（约55px）
        const containerHeight = tableContainerRef.current.clientHeight;
        setTableScrollY(Math.max(containerHeight - 55, 200));
      }
    };
    
    calculateTableHeight();
    window.addEventListener('resize', calculateTableHeight);
    
    // 延迟再计算一次，确保布局完成
    const timer = setTimeout(calculateTableHeight, 100);
    
    return () => {
      window.removeEventListener('resize', calculateTableHeight);
      clearTimeout(timer);
    };
  }, [stats]); // stats 变化时重新计算（因为统计卡片高度可能变化）

  useEffect(() => {
    if (chapters.length > 0) {
      loadStats();
    }
  }, [chapters, loadStats]);

  // 创建/编辑伏笔
  const handleSave = async (values: ForeshadowCreate | ForeshadowUpdate) => {
    try {
      if (currentForeshadow) {
        await foreshadowApi.updateForeshadow(currentForeshadow.id, values as ForeshadowUpdate);
        message.success(t('toast.updateSuccess'));
      } else {
        await foreshadowApi.createForeshadow({
          ...values,
          project_id: projectId!,
        } as ForeshadowCreate);
        message.success(t('toast.createSuccess'));
      }
      setEditModalVisible(false);
      form.resetFields();
      setCurrentForeshadow(null);
      loadForeshadows();
    } catch (error) {
      console.error('保存伏笔失败:', error);
    }
  };

  // 删除伏笔
  const handleDelete = async (id: string) => {
    try {
      await foreshadowApi.deleteForeshadow(id);
      message.success(t('toast.deleteSuccess'));
      loadForeshadows();
    } catch (error) {
      console.error('删除伏笔失败:', error);
    }
  };

  // 标记埋入
  const handlePlant = async (values: { chapter_id: string; hint_text?: string }) => {
    if (!currentForeshadow) return;
    
    const chapter = chapters.find(c => c.id === values.chapter_id);
    if (!chapter) return;
    
    try {
      await foreshadowApi.plantForeshadow(currentForeshadow.id, {
        chapter_id: values.chapter_id,
        chapter_number: chapter.chapter_number,
        hint_text: values.hint_text,
      });
      message.success(t('toast.plantedSuccess'));
      setPlantModalVisible(false);
      plantForm.resetFields();
      setCurrentForeshadow(null);
      loadForeshadows();
    } catch (error) {
      console.error('标记埋入失败:', error);
    }
  };

  // 标记回收
  const handleResolve = async (values: { chapter_id: string; resolution_text?: string; is_partial?: boolean }) => {
    if (!currentForeshadow) return;
    
    const chapter = chapters.find(c => c.id === values.chapter_id);
    if (!chapter) return;
    
    try {
      await foreshadowApi.resolveForeshadow(currentForeshadow.id, {
        chapter_id: values.chapter_id,
        chapter_number: chapter.chapter_number,
        resolution_text: values.resolution_text,
        is_partial: values.is_partial,
      });
      message.success(t('toast.resolvedSuccess'));
      setResolveModalVisible(false);
      resolveForm.resetFields();
      setCurrentForeshadow(null);
      loadForeshadows();
    } catch (error) {
      console.error('标记回收失败:', error);
    }
  };

  // 标记废弃
  const handleAbandon = async (id: string) => {
    try {
      await foreshadowApi.abandonForeshadow(id);
      message.success(t('toast.abandonedSuccess'));
      loadForeshadows();
    } catch (error) {
      console.error('标记废弃失败:', error);
    }
  };

  // 从分析同步
  const handleSync = async () => {
    if (!projectId) return;
    
    setSyncing(true);
    try {
      const result = await foreshadowApi.syncFromAnalysis(projectId, {
        auto_set_planted: true,
      });
      message.success(t('toast.syncSuccess', { added: result.synced_count, skipped: result.skipped_count }));
      setSyncModalVisible(false);
      loadForeshadows();
    } catch (error) {
      console.error('同步失败:', error);
    } finally {
      setSyncing(false);
    }
  };

  // 打开编辑模态框
  const openEditModal = (foreshadow?: Foreshadow) => {
    setCurrentForeshadow(foreshadow || null);
    if (foreshadow) {
      // 确保数组类型字段不为null
      form.setFieldsValue({
        ...foreshadow,
        tags: foreshadow.tags || [],
        related_characters: foreshadow.related_characters || [],
      });
    } else {
      form.resetFields();
    }
    setEditModalVisible(true);
  };

  // 打开详情模态框
  const openDetailModal = (foreshadow: Foreshadow) => {
    setCurrentForeshadow(foreshadow);
    setDetailModalVisible(true);
  };

  // 打开埋入模态框
  const openPlantModal = (foreshadow: Foreshadow) => {
    setCurrentForeshadow(foreshadow);
    plantForm.resetFields();
    setPlantModalVisible(true);
  };

  // 打开回收模态框
  const openResolveModal = (foreshadow: Foreshadow) => {
    setCurrentForeshadow(foreshadow);
    resolveForm.resetFields();
    setResolveModalVisible(true);
  };

  // 计算紧急程度
  const getUrgencyBadge = (foreshadow: Foreshadow) => {
    if (foreshadow.status !== 'planted' || !foreshadow.target_resolve_chapter_number) {
      return null;
    }
    
    const chaptersWithContent = chapters.filter(c => c.content);
    const currentMaxChapter = chaptersWithContent.length > 0
      ? Math.max(...chaptersWithContent.map(c => c.chapter_number))
      : 0;
    
    const remaining = foreshadow.target_resolve_chapter_number - currentMaxChapter;
    
    if (remaining < 0) {
      return <Badge status="error" text={t('urgency.overdue', { num: Math.abs(remaining) })} />;
    } else if (remaining <= 3) {
      return <Badge status="warning" text={t('urgency.remaining', { num: remaining })} />;
    }
    return null;
  };

  // 状态排序优先级
  const statusOrder: Record<ForeshadowStatus, number> = {
    planted: 1,      // 已埋入优先（需要关注回收）
    pending: 2,      // 待埋入次之
    partially_resolved: 3,
    resolved: 4,
    abandoned: 5,
  };

  // 表格列定义
  const columns = [
    {
      title: t('col.status'),
      dataIndex: 'status',
      key: 'status',
      width: 100,
      sorter: (a: Foreshadow, b: Foreshadow) => statusOrder[a.status] - statusOrder[b.status],
      render: (status: ForeshadowStatus) => {
        const config = STATUS_CONFIG[status];
        return (
          <Tag color={config.color} icon={config.icon}>
            {config.label}
          </Tag>
        );
      },
    },
    {
      title: t('col.title'),
      dataIndex: 'title',
      key: 'title',
      ellipsis: true,
      sorter: (a: Foreshadow, b: Foreshadow) => a.title.localeCompare(b.title, sortLocale),
      render: (title: string, record: Foreshadow) => (
        <Space direction="vertical" size={0}>
          <Space>
            <a onClick={() => openDetailModal(record)}>{title}</a>
            {record.is_long_term && (
              <Tag color="purple" style={{ marginLeft: 4 }}>{t('tag.longTerm')}</Tag>
            )}
          </Space>
          {getUrgencyBadge(record)}
        </Space>
      ),
    },
    {
      title: t('col.category'),
      dataIndex: 'category',
      key: 'category',
      width: 80,
      sorter: (a: Foreshadow, b: Foreshadow) => {
        const catA = a.category || '';
        const catB = b.category || '';
        return catA.localeCompare(catB, sortLocale);
      },
      render: (category?: ForeshadowCategory) => {
        if (!category) return '-';
        const config = CATEGORY_CONFIG[category];
        return config ? <Tag color={config.color}>{config.label}</Tag> : category;
      },
    },
    {
      title: t('col.plantChapter'),
      dataIndex: 'plant_chapter_number',
      key: 'plant_chapter_number',
      width: 120,
      sorter: (a: Foreshadow, b: Foreshadow) => {
        const valA = a.plant_chapter_number ?? 999999;
        const valB = b.plant_chapter_number ?? 999999;
        return valA - valB;
      },
      defaultSortOrder: 'ascend' as const,
      render: (num?: number) => num ? t('chapter.chapterN', { num }) : '-',
    },
    {
      title: t('col.planResolve'),
      dataIndex: 'target_resolve_chapter_number',
      key: 'target_resolve_chapter_number',
      width: 120,
      sorter: (a: Foreshadow, b: Foreshadow) => {
        const valA = a.target_resolve_chapter_number ?? 999999;
        const valB = b.target_resolve_chapter_number ?? 999999;
        return valA - valB;
      },
      render: (num?: number) => num ? t('chapter.chapterN', { num }) : '-',
    },
    {
      title: t('col.importance'),
      dataIndex: 'importance',
      key: 'importance',
      width: 100,
      sorter: (a: Foreshadow, b: Foreshadow) => a.importance - b.importance,
      render: (importance: number) => {
        const stars = Math.round(importance * 5);
        return '★'.repeat(stars) + '☆'.repeat(5 - stars);
      },
    },
    {
      title: t('col.source'),
      dataIndex: 'source_type',
      key: 'source_type',
      width: 80,
      sorter: (a: Foreshadow, b: Foreshadow) => {
        const srcA = a.source_type || '';
        const srcB = b.source_type || '';
        return srcA.localeCompare(srcB, sortLocale);
      },
      render: (source?: string) => (
        <Tag color={source === 'analysis' ? 'blue' : 'green'}>
          {source === 'analysis' ? t('source.analysis') : t('source.manual')}
        </Tag>
      ),
    },
    {
      title: t('col.actions'),
      key: 'actions',
      width: 200,
      render: (_: unknown, record: Foreshadow) => (
        <Space size="small">
          <Tooltip title={t('tooltip.viewDetail')}>
            <Button type="text" size="small" icon={<EyeOutlined />} onClick={() => openDetailModal(record)} />
          </Tooltip>
          <Tooltip title={t('tooltip.edit')}>
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEditModal(record)} />
          </Tooltip>
          {record.status === 'pending' && (
            <Tooltip title={t('tooltip.plant')}>
              <Button type="text" size="small" icon={<FlagOutlined />} onClick={() => openPlantModal(record)} />
            </Tooltip>
          )}
          {record.status === 'planted' && (
            <Tooltip title={t('tooltip.resolve')}>
              <Button type="text" size="small" icon={<CheckCircleOutlined />} onClick={() => openResolveModal(record)} />
            </Tooltip>
          )}
          {record.status !== 'abandoned' && record.status !== 'resolved' && (
            <Popconfirm
              title={t('confirm.abandon')}
              onConfirm={() => handleAbandon(record.id)}
            >
              <Tooltip title={t('tooltip.abandon')}>
                <Button type="text" size="small" danger icon={<CloseCircleOutlined />} />
              </Tooltip>
            </Popconfirm>
          )}
          <Popconfirm
            title={t('confirm.delete')}
            onConfirm={() => handleDelete(record.id)}
          >
            <Tooltip title={t('tooltip.delete')}>
              <Button type="text" size="small" danger icon={<DeleteOutlined />} />
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* 统计卡片 */}
      {stats && (
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col span={3}>
            <Card size="small">
              <Statistic title={t('stat.total')} value={stats.total} />
            </Card>
          </Col>
          <Col span={3}>
            <Card size="small">
              <Statistic title={t('stat.pending')} value={stats.pending} valueStyle={{ color: token.colorTextSecondary }} />
            </Card>
          </Col>
          <Col span={3}>
            <Card size="small">
              <Statistic title={t('stat.planted')} value={stats.planted} valueStyle={{ color: token.colorSuccess }} />
            </Card>
          </Col>
          <Col span={3}>
            <Card size="small">
              <Statistic title={t('stat.resolved')} value={stats.resolved} valueStyle={{ color: token.colorPrimary }} />
            </Card>
          </Col>
          <Col span={3}>
            <Card size="small">
              <Statistic title={t('stat.longTerm')} value={stats.long_term_count} valueStyle={{ color: token.colorInfo }} />
            </Card>
          </Col>
          <Col span={3}>
            <Card size="small">
              <Statistic 
                title={t('stat.overdue')} 
                value={stats.overdue_count} 
                valueStyle={{ color: stats.overdue_count > 0 ? token.colorError : token.colorTextSecondary }}
                prefix={stats.overdue_count > 0 ? <WarningOutlined /> : null}
              />
            </Card>
          </Col>
        </Row>
      )}

      {/* 超期提醒 */}
      {stats && stats.overdue_count > 0 && (
        <Alert
          message={t('alert.overdueMsg', { num: stats.overdue_count })}
          description={t('alert.overdueDesc')}
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      {/* 自动同步提示 */}
      <Alert
        message={
          <Space>
            <InfoCircleOutlined />
            <span>{t('alert.autoSync')}</span>
          </Space>
        }
        type="info"
        showIcon={false}
        style={{ marginBottom: 16 }}
        closable
      />

      {/* 工具栏 */}
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Space>
          <Select
            placeholder={t('filter.statusPlaceholder')}
            allowClear
            style={{ width: 120 }}
            value={statusFilter}
            onChange={setStatusFilter}
          >
            {Object.entries(STATUS_CONFIG).map(([key, config]) => (
              <Option key={key} value={key}>{config.label}</Option>
            ))}
          </Select>
          <Select
            placeholder={t('filter.categoryPlaceholder')}
            allowClear
            style={{ width: 100 }}
            value={categoryFilter}
            onChange={setCategoryFilter}
          >
            {Object.entries(CATEGORY_CONFIG).map(([key, config]) => (
              <Option key={key} value={key}>{config.label}</Option>
            ))}
          </Select>
          <Select
            placeholder={t('filter.sourcePlaceholder')}
            allowClear
            style={{ width: 100 }}
            value={sourceFilter}
            onChange={setSourceFilter}
          >
            <Option value="analysis">{t('source.analysis')}</Option>
            <Option value="manual">{t('source.manual')}</Option>
          </Select>
        </Space>
        <Space>
          <Tooltip title={t('tooltip.refresh')}>
            <Button
              icon={<ReloadOutlined spin={loading} />}
              onClick={loadForeshadows}
            />
          </Tooltip>
          <Dropdown
            menu={{
              items: [
                {
                  key: 'sync',
                  icon: <SyncOutlined />,
                  label: t('more.syncItem'),
                  onClick: () => setSyncModalVisible(true),
                },
              ] as MenuProps['items'],
            }}
            placement="bottomRight"
          >
            <Button icon={<MoreOutlined />}>{t('more.button')}</Button>
          </Dropdown>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => openEditModal()}
          >
            {t('form.titleAdd')}
          </Button>
        </Space>
      </div>

      {/* 伏笔列表 - 表格内容可滚动，表头固定 */}
      <div
        ref={tableContainerRef}
        style={{
          flex: 1,
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0, // 重要：让 flex 子元素可以收缩
        }}
      >
        <Table
          dataSource={foreshadows}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={false}
          scroll={{ y: tableScrollY }}
          locale={{
            emptyText: <Empty description={t('common.empty')} />,
          }}
        />
      </div>

      {/* 分页器 - 固定在底部居中 */}
      <div style={{
        padding: '12px 0',
        borderTop: `1px solid ${token.colorBorderSecondary}`,
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        flexShrink: 0,
        background: token.colorBgContainer,
      }}>
        <Pagination
          current={currentPage}
          pageSize={pageSize}
          total={total}
          onChange={(page, size) => {
            setCurrentPage(page);
            if (size !== pageSize) {
              setPageSize(size);
            }
          }}
          showSizeChanger
          showTotal={(total) => t('pagination.total', { total })}
          showQuickJumper
        />
      </div>

      {/* 创建/编辑模态框 */}
      <Modal
        title={currentForeshadow ? t('form.titleEdit') : t('form.titleAdd')}
        open={editModalVisible}
        centered
        onCancel={() => {
          setEditModalVisible(false);
          setCurrentForeshadow(null);
          form.resetFields();
        }}
        onOk={() => form.submit()}
        width={800}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSave}
          initialValues={{
            importance: 0.5,
            strength: 5,
            subtlety: 5,
            is_long_term: false,
            auto_remind: true,
            remind_before_chapters: 5,
            include_in_context: true,
          }}
        >
          <Row gutter={16}>
            <Col span={16}>
              <Form.Item name="title" label={t('form.titleLabel')} rules={[{ required: true, message: t('form.titleRequired') }]}>
                <Input placeholder={t('form.titlePlaceholder')} maxLength={200} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="category" label={t('form.categoryLabel')}>
                <Select placeholder={t('form.categoryPlaceholder')} allowClear>
                  {Object.entries(CATEGORY_CONFIG).map(([key, config]) => (
                    <Option key={key} value={key}>{config.label}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
          </Row>
          
          <Form.Item name="content" label={t('form.contentLabel')} rules={[{ required: true, message: t('form.contentRequired') }]}>
            <TextArea rows={3} placeholder={t('form.contentPlaceholder')} />
          </Form.Item>
          
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item name="plant_chapter_number" label={t('form.plantChapterLabel')}>
                <InputNumber min={1} placeholder={t('form.chapterPlaceholder')} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="target_resolve_chapter_number" label={t('form.planResolveLabel')}>
                <InputNumber min={1} placeholder={t('form.chapterPlaceholder')} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="related_characters" label={t('form.relatedCharsLabel')}>
                <Select
                  mode="multiple"
                  placeholder={t('form.relatedCharsPlaceholder')}
                  optionFilterProp="children"
                  maxTagCount={3}
                >
                  {characters
                    .filter(char => !char.is_organization)
                    .map(char => (
                      <Option key={char.name} value={char.name}>
                        {char.name} {char.role_type ? t('form.charWithRole', { role: char.role_type }) : ''}
                      </Option>
                    ))}
                </Select>
              </Form.Item>
            </Col>
          </Row>
          
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item name="importance" label={t('form.importanceLabel')}>
                <InputNumber min={0} max={1} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="strength" label={t('form.strengthLabel')}>
                <InputNumber min={1} max={10} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="subtlety" label={t('form.subtletyLabel')}>
                <InputNumber min={1} max={10} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="is_long_term" label={t('form.longTermLabel')} valuePropName="checked">
                <Switch checkedChildren={t('form.yes')} unCheckedChildren={t('form.no')} />
              </Form.Item>
            </Col>
          </Row>
          
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="hint_text" label={t('form.hintLabel')}>
                <TextArea rows={2} placeholder={t('form.hintPlaceholder')} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="notes" label={t('form.notesLabel')}>
                <TextArea rows={2} placeholder={t('form.notesPlaceholder')} />
              </Form.Item>
            </Col>
          </Row>
          
          <Divider style={{ margin: '12px 0' }}>{t('form.aiSettingsDivider')}</Divider>
          
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="auto_remind" label={t('form.autoRemindLabel')} valuePropName="checked" style={{ marginBottom: 0 }}>
                <Switch checkedChildren={t('form.on')} unCheckedChildren={t('form.off')} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="include_in_context" label={t('form.includeInContextLabel')} valuePropName="checked" style={{ marginBottom: 0 }}>
                <Switch checkedChildren={t('form.yes')} unCheckedChildren={t('form.no')} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="remind_before_chapters" label={t('form.remindChaptersLabel')} style={{ marginBottom: 0 }}>
                <InputNumber min={1} max={20} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* 详情模态框 */}
      <Modal
        title={t('detail.title')}
        open={detailModalVisible}
        centered
        onCancel={() => {
          setDetailModalVisible(false);
          setCurrentForeshadow(null);
        }}
        footer={[
          <Button key="close" onClick={() => setDetailModalVisible(false)}>
            {t('detail.close')}
          </Button>,
          <Button key="edit" type="primary" onClick={() => {
            setDetailModalVisible(false);
            openEditModal(currentForeshadow!);
          }}>
            {t('detail.edit')}
          </Button>,
        ]}
        width={600}
      >
        {currentForeshadow && (
          <div>
            <Row gutter={[16, 16]}>
              <Col span={24}>
                <h3>{currentForeshadow.title}</h3>
                <Space>
                  <Tag color={STATUS_CONFIG[currentForeshadow.status].color}>
                    {STATUS_CONFIG[currentForeshadow.status].label}
                  </Tag>
                  {currentForeshadow.is_long_term && <Tag color="purple">{t('detail.longTermTag')}</Tag>}
                  {currentForeshadow.category && CATEGORY_CONFIG[currentForeshadow.category] && (
                    <Tag color={CATEGORY_CONFIG[currentForeshadow.category].color}>
                      {CATEGORY_CONFIG[currentForeshadow.category].label}
                    </Tag>
                  )}
                </Space>
              </Col>
              
              <Col span={24}>
                <strong>{t('detail.contentLabel')}</strong>
                <p style={{ marginTop: 8, whiteSpace: 'pre-wrap' }}>{currentForeshadow.content}</p>
              </Col>
              
              {currentForeshadow.hint_text && (
                <Col span={24}>
                  <strong>{t('detail.hintLabel')}</strong>
                  <p style={{ marginTop: 8, whiteSpace: 'pre-wrap', color: token.colorTextSecondary }}>
                    {currentForeshadow.hint_text}
                  </p>
                </Col>
              )}
              
              {currentForeshadow.resolution_text && (
                <Col span={24}>
                  <strong>{t('detail.revealLabel')}</strong>
                  <p style={{ marginTop: 8, whiteSpace: 'pre-wrap', color: token.colorTextSecondary }}>
                    {currentForeshadow.resolution_text}
                  </p>
                </Col>
              )}
              
              <Col span={12}>
                <strong>{t('detail.plantChapterLabel')}</strong> {currentForeshadow.plant_chapter_number ? t('chapter.chapterN', { num: currentForeshadow.plant_chapter_number }) : t('detail.notSet')}
              </Col>
              <Col span={12}>
                <strong>{t('detail.planResolveLabel')}</strong> {currentForeshadow.target_resolve_chapter_number ? t('chapter.chapterN', { num: currentForeshadow.target_resolve_chapter_number }) : t('detail.notSet')}
              </Col>
              
              {currentForeshadow.actual_resolve_chapter_number && (
                <Col span={24}>
                  <strong>{t('detail.actualResolveLabel')}</strong> {t('chapter.chapterN', { num: currentForeshadow.actual_resolve_chapter_number })}
                </Col>
              )}
              
              <Col span={8}>
                <strong>{t('detail.importanceLabel')}</strong> {'★'.repeat(Math.round(currentForeshadow.importance * 5))}
              </Col>
              <Col span={8}>
                <strong>{t('detail.strengthLabel')}</strong> {currentForeshadow.strength}/10
              </Col>
              <Col span={8}>
                <strong>{t('detail.subtletyLabel')}</strong> {currentForeshadow.subtlety}/10
              </Col>
              
              {currentForeshadow.related_characters && currentForeshadow.related_characters.length > 0 && (
                <Col span={24}>
                  <strong>{t('detail.relatedCharsLabel')}</strong>
                  <div style={{ marginTop: 4 }}>
                    {currentForeshadow.related_characters.map((name, idx) => (
                      <Tag key={idx}>{name}</Tag>
                    ))}
                  </div>
                </Col>
              )}
              
              {currentForeshadow.notes && (
                <Col span={24}>
                  <strong>{t('detail.notesLabel')}</strong>
                  <p style={{ marginTop: 8, color: token.colorTextSecondary }}>{currentForeshadow.notes}</p>
                </Col>
              )}
              
              <Col span={24}>
                <strong>{t('detail.sourceLabel')}</strong> {currentForeshadow.source_type === 'analysis' ? t('detail.sourceAnalysis') : t('detail.sourceManual')}
              </Col>
            </Row>
          </div>
        )}
      </Modal>

      {/* 标记埋入模态框 */}
      <Modal
        title={t('plant.title')}
        open={plantModalVisible}
        centered
        onCancel={() => {
          setPlantModalVisible(false);
          setCurrentForeshadow(null);
          plantForm.resetFields();
        }}
        onOk={() => plantForm.submit()}
        destroyOnHidden
      >
        <Form form={plantForm} layout="vertical" onFinish={handlePlant}>
          <Form.Item name="chapter_id" label={t('plant.selectChapter')} rules={[{ required: true, message: t('plant.selectRequired') }]}>
            <Select placeholder={t('plant.selectPlaceholder')}>
              {chapters.map(chapter => (
                <Option key={chapter.id} value={chapter.id}>
                  {t('chapter.chapterWithTitle', { num: chapter.chapter_number, title: chapter.title })}
                </Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item name="hint_text" label={t('form.hintOptional')}>
            <TextArea rows={3} placeholder={t('form.plantHintPlaceholder')} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 标记回收模态框 */}
      <Modal
        title={t('resolve.title')}
        open={resolveModalVisible}
        centered
        onCancel={() => {
          setResolveModalVisible(false);
          setCurrentForeshadow(null);
          resolveForm.resetFields();
        }}
        onOk={() => resolveForm.submit()}
        destroyOnHidden
      >
        <Form form={resolveForm} layout="vertical" onFinish={handleResolve}>
          <Form.Item name="chapter_id" label={t('resolve.selectChapter')} rules={[{ required: true, message: t('resolve.selectRequired') }]}>
            <Select placeholder={t('resolve.selectPlaceholder')}>
              {chapters.map(chapter => (
                <Option key={chapter.id} value={chapter.id}>
                  {t('chapter.chapterWithTitle', { num: chapter.chapter_number, title: chapter.title })}
                </Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item name="resolution_text" label={t('form.resolveTextOptional')}>
            <TextArea rows={3} placeholder={t('form.resolveHintPlaceholder')} />
          </Form.Item>
          <Form.Item name="is_partial" label={t('form.partialResolveLabel')} valuePropName="checked">
            <Switch checkedChildren={t('form.partial')} unCheckedChildren={t('form.complete')} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 同步模态框 */}
      <Modal
        title={t('sync.title')}
        open={syncModalVisible}
        centered
        onCancel={() => setSyncModalVisible(false)}
        onOk={handleSync}
        confirmLoading={syncing}
        okText={t('sync.okText')}
      >
        <Alert
          message={t('sync.alertTitle')}
          description={t('sync.alertDesc')}
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
        />
        <p>{t('sync.body')}</p>
        <ul>
          <li>{t('sync.bullet1')}</li>
          <li>{t('sync.bullet2')}</li>
          <li>{t('sync.bullet3')}</li>
        </ul>
      </Modal>
    </div>
  );
}
