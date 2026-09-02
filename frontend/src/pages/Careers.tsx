import { useState, useEffect, useCallback } from 'react';
import { Button, Modal, Form, Input, Select, message, Row, Col, Empty, Tabs, Card, Tag, Space, Divider, Typography, InputNumber } from 'antd';
import { ThunderboltOutlined, PlusOutlined, EditOutlined, DeleteOutlined, TrophyOutlined } from '@ant-design/icons';
import { useParams } from 'react-router-dom';
import api from '../services/api';
import SSEProgressModal from '../components/SSEProgressModal';
import { eventBus, EventNames } from '../store/eventBus';
import { Trans, useTranslation } from 'react-i18next';

const { TextArea } = Input;
const { Title, Text, Paragraph } = Typography;

interface CareerStage {
    level: number;
    name: string;
    description?: string;
}

interface Career {
    id: string;
    project_id: string;
    name: string;
    type: 'main' | 'sub';
    description?: string;
    category?: string;
    stages: CareerStage[];
    max_stage: number;
    requirements?: string;
    special_abilities?: string;
    worldview_rules?: string;
    source: string;
}

export default function Careers() {
    const { t } = useTranslation('careers');
    const { projectId } = useParams<{ projectId: string }>();
    const [mainCareers, setMainCareers] = useState<Career[]>([]);
    const [subCareers, setSubCareers] = useState<Career[]>([]);
    const [, setLoading] = useState(true);
    const [isModalOpen, setIsModalOpen] = useState(false);
    const [isAIModalOpen, setIsAIModalOpen] = useState(false);
    const [editingCareer, setEditingCareer] = useState<Career | null>(null);
    const [form] = Form.useForm();
    const [aiForm] = Form.useForm();
    const [modal, contextHolder] = Modal.useModal();

    // AI生成状态
    const [aiGenerating, setAiGenerating] = useState(false);
    const [aiProgress, setAiProgress] = useState(0);
    const [aiMessage, setAiMessage] = useState('');

    const fetchCareers = useCallback(async () => {
        try {
            setLoading(true);
            const response = await api.get('/careers', {
                params: { project_id: projectId }
            }) as { main_careers?: Career[]; sub_careers?: Career[] };
            setMainCareers(response.main_careers || []);
            setSubCareers(response.sub_careers || []);
        } catch (error: unknown) {
            console.error('获取职业列表失败:', error);
        } finally {
            setLoading(false);
        }
    }, [projectId]);

    useEffect(() => {
        if (projectId) {
            fetchCareers();
        }
    }, [projectId, fetchCareers]);

    useEffect(() => {
        const handleTaskSettled = (payload?: unknown) => {
            if (!payload || typeof payload !== 'object') return;
            const data = payload as { projectId?: string; resources?: string[] };
            if (data.projectId && data.projectId !== projectId) return;
            if (data.resources?.includes('careers')) {
                void fetchCareers();
            }
        };
        eventBus.on(EventNames.BACKGROUND_TASK_SETTLED, handleTaskSettled);
        return () => eventBus.off(EventNames.BACKGROUND_TASK_SETTLED, handleTaskSettled);
    }, [fetchCareers, projectId]);

    const handleOpenModal = (career?: Career) => {
        if (career) {
            setEditingCareer(career);
            form.setFieldsValue({
                ...career,
                stages: career.stages.map(s => `${s.level}. ${s.name}${s.description ? ` - ${s.description}` : ''}`).join('\n')
            });
        } else {
            setEditingCareer(null);
            form.resetFields();
        }
        setIsModalOpen(true);
    };

    interface CareerFormValues {
        name: string;
        type: 'main' | 'sub';
        description?: string;
        category?: string;
        stages?: string;
        requirements?: string;
        special_abilities?: string;
        worldview_rules?: string;
    }

    const handleSubmit = async (values: CareerFormValues) => {
        try {
            // 解析阶段数据
            const stagesText = values.stages || '';
            const stages: CareerStage[] = stagesText.split('\n')
                .filter((line: string) => line.trim())
                .map((line: string, index: number) => {
                    const match = line.match(/^(\d+)\.\s*([^-]+)(?:\s*-\s*(.*))?$/);
                    if (match) {
                        return {
                            level: parseInt(match[1]),
                            name: match[2].trim(),
                            description: match[3]?.trim() || ''
                        };
                    }
                    return {
                        level: index + 1,
                        name: line.trim(),
                        description: ''
                    };
                });

            const data = {
                ...values,
                stages,
                max_stage: stages.length
            };

            if (editingCareer) {
                await api.put(`/careers/${editingCareer.id}`, data);
                message.success(t('toast.updateSuccess'));
            } else {
                await api.post('/careers', {
                    ...data,
                    project_id: projectId,
                    source: 'manual'
                });
                message.success(t('toast.createSuccess'));
            }

            setIsModalOpen(false);
            form.resetFields();
            fetchCareers();
        } catch (error: unknown) {
            const axiosError = error as { response?: { data?: { detail?: string } } };
            message.error(axiosError.response?.data?.detail || t('toast.actionFailed'));
        }
    };

    const handleDelete = async (id: string) => {
        modal.confirm({
            title: t('delete.title'),
            content: t('delete.content'),
            centered: true,
            onOk: async () => {
                try {
                    await api.delete(`/careers/${id}`);
                    message.success(t('toast.deleteSuccess'));
                    fetchCareers();
                } catch (error: unknown) {
                    const axiosError = error as { response?: { data?: { detail?: string } } };
                    message.error(axiosError.response?.data?.detail || t('toast.deleteFailed'));
                }
            }
        });
    };

    const handleAIGenerate = async (values: {
        main_career_count: number;
        sub_career_count: number;
        user_requirements?: string;
    }) => {
        setIsAIModalOpen(false);
        setAiGenerating(true);
        setAiProgress(0);
        setAiMessage(t('ai.startMessage'));

        try {
            const userRequirements = values.user_requirements?.trim() || '';

            // 使用 fetch + POST 替代 EventSource GET，避免 URL 长度限制
            const response = await fetch('/api/careers/generate-system', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                credentials: 'include',
                body: JSON.stringify({
                    project_id: projectId || '',
                    main_career_count: values.main_career_count,
                    sub_career_count: values.sub_career_count,
                    user_requirements: userRequirements,
                    enable_mcp: false
                })
            });

            if (!response.ok || !response.body) {
                setAiGenerating(false);
                message.error(t('ai.requestFailed', { status: response.status }));
                return;
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        try {
                            const data = JSON.parse(line.slice(6));

                            if (data.type === 'progress') {
                                setAiProgress(data.progress || 0);
                                setAiMessage(data.message || '');
                            } else if (data.type === 'done') {
                                setTimeout(() => {
                                    setAiGenerating(false);
                                    message.success(t('ai.doneMessage'));
                                    fetchCareers();
                                }, 1000);
                            } else if (data.type === 'error') {
                                setAiGenerating(false);
                                message.error(data.error || data.message || t('ai.generateFailed'));
                            }
                        } catch {
                            // 忽略非JSON行（如心跳注释）
                        }
                    }
                }
            }

            setAiGenerating(false);
        } catch (err: unknown) {
            setAiGenerating(false);
            const error = err as Error;
            message.error(error.message || t('ai.startFailed'));
        }
    };

    const renderCareerCard = (career: Career) => (
        <Card
            key={career.id}
            title={
                <Space>
                    <TrophyOutlined />
                    {career.name}
                    <Tag color={career.source === 'ai' ? 'blue' : 'default'}>
                        {career.source === 'ai' ? t('tag.aiGenerated') : t('tag.manual')}
                    </Tag>
                    {career.category && <Tag>{career.category}</Tag>}
                </Space>
            }
            extra={
                <Space>
                    <Button size="small" icon={<EditOutlined />} onClick={() => handleOpenModal(career)} />
                    <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(career.id)} />
                </Space>
            }
            style={{ marginBottom: 16 }}
        >
            <Paragraph ellipsis={{ rows: 2 }}>{career.description || t('card.noDescription')}</Paragraph>
            <Divider style={{ margin: '12px 0' }} />
            <Text strong>{t('card.stageSystem', { n: career.max_stage })}</Text>
            <div style={{ maxHeight: 120, overflowY: 'auto', marginTop: 8 }}>
                {career.stages.slice(0, 5).map(stage => (
                    <div key={stage.level} style={{ marginLeft: 16, marginBottom: 4 }}>
                        <Text type="secondary">{stage.level}. {stage.name}</Text>
                        {stage.description && <Text type="secondary" style={{ fontSize: 12 }}> - {stage.description}</Text>}
                    </div>
                ))}
                {career.stages.length > 5 && (
                    <Text type="secondary" style={{ marginLeft: 16 }}>{t('card.moreStages', { n: career.stages.length - 5 })}</Text>
                )}
            </div>
            {career.special_abilities && (
                <>
                    <Divider style={{ margin: '12px 0' }} />
                    <Text strong>{t('card.specialAbilities')}</Text>
                    <Paragraph ellipsis={{ rows: 2 }} style={{ marginTop: 4 }}>{career.special_abilities}</Paragraph>
                </>
            )}
        </Card>
    );

    const tabItems = [
        {
            key: 'main',
            label: t('tabs.main', { n: mainCareers.length }),
            children: mainCareers.length > 0 ? (
                <div>{mainCareers.map(renderCareerCard)}</div>
            ) : (
                <Empty description={t('empty.main')} />
            )
        },
        {
            key: 'sub',
            label: t('tabs.sub', { n: subCareers.length }),
            children: subCareers.length > 0 ? (
                <div>{subCareers.map(renderCareerCard)}</div>
            ) : (
                <Empty description={t('empty.sub')} />
            )
        }
    ];

    return (
        <>
            {contextHolder}
            <div style={{
            height: '100%',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden'
        }}>
            {/* 固定头部 */}
            <div style={{
                padding: '16px 16px 0 16px',
                flexShrink: 0
            }}>
                <div style={{
                    marginBottom: 16,
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    flexWrap: 'wrap',
                    gap: '12px'
                }}>
                    <Title level={3} style={{ margin: 0 }}>
                        <TrophyOutlined style={{ marginRight: 8 }} />
                        {t('page.title')}
                    </Title>
                    <Space wrap>
                        <Button
                            type="dashed"
                            icon={<ThunderboltOutlined />}
                            onClick={() => {
                                aiForm.resetFields();
                                setIsAIModalOpen(true);
                            }}
                        >
                            {t('page.aiGenerate')}
                        </Button>
                        <Button
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() => handleOpenModal()}
                        >
                            {t('page.addCareer')}
                        </Button>
                    </Space>
                </div>
            </div>

            {/* 可滚动的内容区域 */}
            <div style={{
                flex: 1,
                overflow: 'auto',
                padding: '0 16px 16px 16px'
            }}>
                <Tabs items={tabItems} />
            </div>

            {/* 创建/编辑对话框 */}
            <Modal
                title={editingCareer ? t('form.editTitle') : t('form.addTitle')}
                open={isModalOpen}
                onCancel={() => {
                    setIsModalOpen(false);
                    form.resetFields();
                }}
                footer={null}
                width={700}
            >
                <Form form={form} layout="vertical" onFinish={handleSubmit}>
                    <Row gutter={16}>
                        <Col span={16}>
                            <Form.Item label={t('form.name')} name="name" rules={[{ required: true }]}>
                                <Input placeholder={t('form.namePlaceholder')} />
                            </Form.Item>
                        </Col>
                        <Col span={8}>
                            <Form.Item label={t('form.type')} name="type" rules={[{ required: true }]} initialValue="main">
                                <Select>
                                    <Select.Option value="main">{t('form.typeMain')}</Select.Option>
                                    <Select.Option value="sub">{t('form.typeSub')}</Select.Option>
                                </Select>
                            </Form.Item>
                        </Col>
                    </Row>

                    <Form.Item label={t('form.description')} name="description">
                        <TextArea rows={2} placeholder={t('form.descriptionPlaceholder')} />
                    </Form.Item>

                    <Form.Item label={t('form.category')} name="category">
                        <Input placeholder={t('form.categoryPlaceholder')} />
                    </Form.Item>

                    <Form.Item label={t('form.stages')} name="stages" tooltip={t('form.stagesTooltip')}>
                        <TextArea
                            rows={8}
                            placeholder={t('form.stagesPlaceholder')}
                        />
                    </Form.Item>

                    <Form.Item label={t('form.requirements')} name="requirements">
                        <TextArea rows={2} placeholder={t('form.requirementsPlaceholder')} />
                    </Form.Item>

                    <Form.Item label={t('form.specialAbilities')} name="special_abilities">
                        <TextArea rows={2} placeholder={t('form.specialAbilitiesPlaceholder')} />
                    </Form.Item>

                    <Form.Item label={t('form.worldviewRules')} name="worldview_rules">
                        <TextArea rows={2} placeholder={t('form.worldviewRulesPlaceholder')} />
                    </Form.Item>

                    <Form.Item>
                        <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
                            <Button onClick={() => setIsModalOpen(false)}>{t('buttons.cancel')}</Button>
                            <Button type="primary" htmlType="submit">
                                {editingCareer ? t('buttons.update') : t('buttons.create')}
                            </Button>
                        </Space>
                    </Form.Item>
                </Form>
            </Modal>

            {/* AI生成对话框 */}
            <Modal
                title={t('ai.modalTitle')}
                open={isAIModalOpen}
                onCancel={() => setIsAIModalOpen(false)}
                footer={null}
            >
                <Form form={aiForm} layout="vertical" onFinish={handleAIGenerate}>
                    <Paragraph type="secondary">
                        <Trans ns="careers" i18nKey="ai.modalDesc" components={{ br: <br /> }} />
                    </Paragraph>
                    <Divider style={{ margin: '12px 0' }} />
                    <Form.Item label={t('ai.mainCount')} name="main_career_count" initialValue={3}>
                        <InputNumber min={1} max={10} style={{ width: '100%' }} />
                    </Form.Item>
                    <Form.Item label={t('ai.subCount')} name="sub_career_count" initialValue={5}>
                        <InputNumber min={0} max={15} style={{ width: '100%' }} />
                    </Form.Item>
                    <Form.Item
                        label={t('ai.userRequirements')}
                        name="user_requirements"
                        rules={[{ max: 500, message: t('ai.userRequirementsMax') }]}
                        extra={t('ai.userRequirementsExtra')}
                    >
                        <TextArea
                            rows={4}
                            showCount
                            maxLength={500}
                            placeholder={t('ai.userRequirementsPlaceholder')}
                        />
                    </Form.Item>
                    <Form.Item>
                        <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
                            <Button onClick={() => setIsAIModalOpen(false)}>{t('buttons.cancel')}</Button>
                            <Button type="primary" icon={<ThunderboltOutlined />} htmlType="submit">
                                {t('ai.start')}
                            </Button>
                        </Space>
                    </Form.Item>
                </Form>
            </Modal>

            {/* AI生成进度 */}
            <SSEProgressModal
                visible={aiGenerating}
                progress={aiProgress}
                message={aiMessage}
                title={t('ai.progressTitle')}
                onCancel={() => setAiGenerating(false)}
            />
            </div>
        </>
    );
}
