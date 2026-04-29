import { useState, type ReactNode } from 'react';
import { Card, Row, Col, Typography, Image, Divider, Modal, Button, theme } from 'antd';
import {
    HeartOutlined,
    CheckCircleOutlined,
    FileTextOutlined,
    RocketOutlined,
    MessageOutlined,
    // StarOutlined,
    WechatOutlined
} from '@ant-design/icons';

const { Title, Paragraph, Text } = Typography;

interface SponsorOption {
    amount: number | string;
    label: string;
    image: string;
    description: string;
}

interface SponsorBenefit {
    icon: ReactNode;
    title: string;
    description: string;
    price?: string;
}

const sponsorOptions: SponsorOption[] = [
    { amount: 5, label: '🌶️ 一包辣条', image: '/5.png', description: '¥5' },
    { amount: 10, label: '🍱 一顿拼好饭', image: '/10.png', description: '¥10' },
    { amount: 20, label: '☕ 一杯咖啡', image: '/20.png', description: '¥20' },
    { amount: 50, label: '🍖 一次烧烤', image: '/50.png', description: '¥50' },
    { amount: 99, label: '🍲 一顿海底捞', image: '/99.png', description: '¥99' },
];

const benefits: SponsorBenefit[] = [
    {
        icon: <WechatOutlined style={{ fontSize: '32px', color: 'var(--ant-color-primary)' }} />,
        title: '加入赞助群',
        description: '进入内部群，获取项目第一手更新消息',
        price: '（🌶️ 一包辣条）'
    },
    {
        icon: <FileTextOutlined style={{ fontSize: '32px', color: 'var(--ant-color-primary)' }} />,
        title: '优先需求响应',
        description: '您的功能需求和问题反馈将获得优先处理',
        price: '（🌶️ 一包辣条）'
    },
    {
        icon: <RocketOutlined style={{ fontSize: '32px', color: 'var(--ant-color-success)' }} />,
        title: 'Windows一键启动',
        description: '获取免安装一键启动包，开箱即可使用',
        price: '（🌶️ 一包辣条）'
    },
    {
        icon: <MessageOutlined style={{ fontSize: '32px', color: 'var(--ant-color-warning)' }} />,
        title: '专属技术支持',
        description: '获得远程协助和配置指导',
        price: '（☕ 一杯咖啡）'
    }
];

export default function Sponsor() {
    const [modalVisible, setModalVisible] = useState(false);
    const [selectedOption, setSelectedOption] = useState<SponsorOption | null>(null);
    const { token } = theme.useToken();
    const alphaColor = (color: string, alpha: number) =>
        `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;

    const handleCardClick = (option: SponsorOption) => {
        setSelectedOption(option);
        setModalVisible(true);
    };

    return (
        <div style={{
            height: '100%',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden'
        }}>
            <div style={{
                flex: 1,
                overflowY: 'auto',
                overflowX: 'hidden',
                // padding: 'clamp(16px, 3vh, 24px) clamp(12px, 2vw, 16px)'
            }}>
                <div style={{
                    // maxWidth: '1200px',
                    height: '100%',
                    margin: '0 auto',
                    width: '100%',
                    display: 'flex',
                    flexDirection: 'column',
                    minHeight: 'fit-content'
                }}>
                    {/* 头部标题区域 */}
                    <div style={{ textAlign: 'center', marginBottom: 'clamp(20px, 4vh, 32px)' }}>
                        <div style={{
                            padding: 'clamp(12px, 2vh, 16px)',
                            background: token.colorPrimary,
                            borderRadius: '12px',
                            color: token.colorWhite
                        }}>
                            <Title level={1} style={{ color: token.colorWhite, marginBottom: '8px', fontSize: 'clamp(24px, 5vw, 32px)', fontWeight: 'bold' }}>
                                赞助 MuMuAINovel
                            </Title>
                            <Text type="secondary" style={{ color: token.colorWhite, fontSize: 'clamp(11px, 2vw, 13px)', letterSpacing: '2px' }}>
                                SUPPORT MuMuAINovel
                            </Text>
                            <Title level={4} style={{ color: token.colorWhite, marginTop: '8px', marginBottom: '8px' }}>
                                📚 MuMuAINovel - 基于 AI 的智能小说创作助手
                            </Title>
                        </div>
                    </div>

                    {/* 赞助专属权益 */}
                    <div style={{ marginBottom: 'clamp(24px, 4vh, 32px)' }}>
                        <Title level={3} style={{ textAlign: 'center', marginBottom: 'clamp(16px, 3vh, 20px)', fontSize: 'clamp(18px, 3vw, 24px)' }}>
                            <CheckCircleOutlined style={{ color: token.colorSuccess, marginRight: '8px' }} />
                            赞助专属权益
                        </Title>

                        <Row
                            gutter={[{ xs: 8, sm: 12, md: 16 }, { xs: 8, sm: 12, md: 16 }]}
                            wrap={false}
                            style={{ overflowX: 'auto', paddingBottom: '4px' }}
                        >
                            {benefits.map((benefit, index) => (
                                <Col key={index} flex="1" style={{ minWidth: '200px' }}>
                                    <Card
                                        hoverable
                                        style={{
                                            height: '100%',
                                            textAlign: 'center',
                                            borderRadius: '10px',
                                            boxShadow: `0 2px 8px ${alphaColor(token.colorTextBase, 0.12)}`
                                        }}
                                        styles={{
                                            body: { padding: 'clamp(16px, 3vh, 20px) clamp(12px, 2vw, 16px)' }
                                        }}
                                    >
                                        <div style={{ marginBottom: '12px' }}>
                                            {benefit.icon}
                                        </div>
                                        <Title level={5} style={{ marginBottom: '8px', fontSize: 'clamp(14px, 2.5vw, 16px)' }}>{benefit.title}</Title>
                                        <Paragraph style={{ color: token.colorTextSecondary, marginBottom: 0, fontSize: 'clamp(12px, 2vw, 13px)' }}>
                                            {benefit.description}
                                        </Paragraph>
                                        {benefit.price && (
                                            <Paragraph style={{ color: token.colorWarning, margin: '4px 0 0', fontSize: 'clamp(12px, 2vw, 13px)', fontWeight: 600 }}>
                                                {benefit.price}
                                            </Paragraph>
                                        )}
                                    </Card>
                                </Col>
                            ))}
                        </Row>
                    </div>

                    {/* 选择金额 */}
                    <div>
                        <Title level={3} style={{ textAlign: 'center', marginBottom: 'clamp(16px, 3vh, 20px)', fontSize: 'clamp(18px, 3vw, 24px)' }}>
                            <HeartOutlined style={{ color: token.colorError, marginRight: '8px' }} />
                            选择金额
                        </Title>

                        <Row gutter={[{ xs: 8, sm: 12, md: 16 }, { xs: 8, sm: 12, md: 16 }]} justify="center">
                            {sponsorOptions.map((option, index) => (
                                <Col xs={12} sm={8} md={6} lg={6} xl={4} key={index}>
                                    <Card
                                        hoverable
                                        onClick={() => handleCardClick(option)}
                                        style={{
                                            textAlign: 'center',
                                            borderRadius: '10px',
                                            boxShadow: `0 2px 8px ${alphaColor(token.colorTextBase, 0.12)}`,
                                            cursor: 'pointer',
                                            transition: 'all 0.3s',
                                            border: `2px solid ${token.colorBorder}`
                                        }}
                                        styles={{
                                            body: { padding: 'clamp(16px, 3vh, 20px) clamp(10px, 2vw, 12px)' }
                                        }}
                                        onMouseEnter={(e) => {
                                            e.currentTarget.style.transform = 'translateY(-8px)';
                                            e.currentTarget.style.boxShadow = `0 8px 24px ${alphaColor(token.colorPrimary, 0.3)}`;
                                            e.currentTarget.style.borderColor = token.colorPrimary;
                                        }}
                                        onMouseLeave={(e) => {
                                            e.currentTarget.style.transform = 'translateY(0)';
                                            e.currentTarget.style.boxShadow = `0 2px 8px ${alphaColor(token.colorTextBase, 0.12)}`;
                                            e.currentTarget.style.borderColor = token.colorBorder;
                                        }}
                                    >
                                        <Title level={3} style={{
                                            color: token.colorPrimary,
                                            marginBottom: '4px',
                                            fontSize: 'clamp(20px, 4vw, 28px)',
                                            fontWeight: 'bold'
                                        }}>
                                            {option.description}
                                        </Title>
                                        <Text style={{ fontSize: 'clamp(12px, 2vw, 14px)', color: token.colorTextSecondary }}>
                                            {option.label}
                                        </Text>
                                    </Card>
                                </Col>
                            ))}
                        </Row>
                    </div>

                    <Divider style={{ margin: 'clamp(16px, 3vh, 18px) 0' }} />

                    {/* 感谢文案 */}
                    <div style={{
                        textAlign: 'center',
                        padding: 'clamp(16px, 3vw, 20px)',
                        background: token.colorFillQuaternary,
                        borderRadius: '10px',
                        marginTop: 'auto'
                    }}>
                        <Title level={4} style={{ marginBottom: '12px', fontSize: 'clamp(16px, 3vw, 20px)' }}>
                            💖 感谢您对 MuMuAINovel 项目的支持
                        </Title>
                        <Paragraph style={{ fontSize: 'clamp(12px, 2vw, 14px)', color: token.colorTextSecondary, marginBottom: '12px' }}>
                            您的赞助将是我持续更新项目的动力，为大家提供更好的AI小说创作体验!
                        </Paragraph>
                        {/* <div style={{ fontSize: 'clamp(18px, 3vw, 24px)' }}>
                            <StarOutlined style={{ color: token.colorWarning, margin: '0 4px' }} />
                            <StarOutlined style={{ color: token.colorWarning, margin: '0 4px' }} />
                            <StarOutlined style={{ color: token.colorWarning, margin: '0 4px' }} />
                            <StarOutlined style={{ color: token.colorWarning, margin: '0 4px' }} />
                            <StarOutlined style={{ color: token.colorWarning, margin: '0 4px' }} />
                        </div> */}
                    </div>
                </div>
            </div>

            {/* 二维码弹窗 */}
            <Modal
                title={
                    <div style={{ textAlign: 'center' }}>
                        <Title level={3} style={{ marginBottom: '8px' }}>
                            {selectedOption?.description} {selectedOption?.label}
                        </Title>
                        <Text type="secondary">请使用微信扫码支付</Text>
                    </div>
                }
                open={modalVisible}
                onCancel={() => setModalVisible(false)}
                footer={[
                    <Button key="close" type="primary" onClick={() => setModalVisible(false)}>
                        关闭
                    </Button>
                ]}
                width={400}
                centered
            >
                <div style={{ textAlign: 'center', padding: '20px 0' }}>
                    <Image
                        src={selectedOption?.image}
                        alt={`${selectedOption?.description}赞助码`}
                        style={{
                            maxWidth: '280px',
                            borderRadius: '8px',
                            border: `1px solid ${token.colorBorderSecondary}`
                        }}
                        preview={false}
                    />
                    <Paragraph style={{ marginTop: '20px', color: token.colorTextSecondary }}>
                        扫描二维码完成支付
                    </Paragraph>
                    <Paragraph style={{ color: token.colorTextTertiary, fontSize: '12px' }}>
                        支付后可添加微信/QQ联系我们获取权益
                    </Paragraph>
                </div>
            </Modal>
        </div>
    );
}