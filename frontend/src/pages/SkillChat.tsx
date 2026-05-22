import React, { useState, useEffect, useRef } from 'react';
import { Card, Input, Button, Tag, List, Typography, Space, Spin, message, Collapse } from 'antd';
import { SendOutlined, RobotOutlined, UserOutlined, ThunderboltOutlined } from '@ant-design/icons';
import axios from 'axios';
// 使用简单的文本渲染替代 react-markdown
const MarkdownRender: React.FC<{ content: string }> = ({ content }) => {
  return <div style={{ whiteSpace: 'pre-wrap' }}>{content}</div>;
};

const { TextArea } = Input;
const { Title, Text, Paragraph } = Typography;

interface Skill {
  template_key: string;
  template_name: string;
  category: string;
  description: string;
  triggers: string[];
}

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

const SkillChat: React.FC = () => {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selectedSkill, setSelectedSkill] = useState<Skill | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [loading, setLoading] = useState(false);
  const [skillsLoading, setSkillsLoading] = useState(true);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    fetchSkills();
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const fetchSkills = async () => {
    try {
      const response = await axios.get('/api/skills/list');
      setSkills(response.data);
    } catch (error) {
      message.error('加载 Skill 列表失败');
    } finally {
      setSkillsLoading(false);
    }
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const handleSkillSelect = (skill: Skill) => {
    setSelectedSkill(skill);
    setMessages([]);
  };

  const handleSend = async () => {
    if (!inputValue.trim() || !selectedSkill || loading) return;

    const userMessage = inputValue.trim();
    setInputValue('');
    const newMessages: ChatMessage[] = [...messages, { role: 'user', content: userMessage }];
    setMessages(newMessages);
    setLoading(true);

    // 添加空的助手消息占位
    const assistantMsg: ChatMessage = { role: 'assistant', content: '' };
    setMessages([...newMessages, assistantMsg]);

    try {
      abortControllerRef.current = new AbortController();
      const response = await fetch('/api/skills/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          skill_key: selectedSkill.template_key,
          message: userMessage,
          history: messages.map(m => ({ role: m.role, content: m.content })),
        }),
        signal: abortControllerRef.current.signal,
      });

      if (!response.ok) throw new Error('请求失败');

      const reader = response.body?.getReader();
      if (!reader) throw new Error('无法读取响应流');

      const decoder = new TextDecoder();
      let accumulated = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const text = decoder.decode(value, { stream: true });
        const lines = text.split('\n');

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));
              if (data.type === 'chunk') {
                accumulated += data.content;
                setMessages(prev => {
                  const updated = [...prev];
                  updated[updated.length - 1] = { role: 'assistant', content: accumulated };
                  return updated;
                });
              } else if (data.type === 'error') {
                message.error(data.error || '生成失败');
              }
            } catch {}
          }
        }
      }
    } catch (error: any) {
      if (error.name !== 'AbortError') {
        message.error('请求失败，请检查 AI 配置');
        setMessages(prev => {
          const updated = [...prev];
          if (updated.length > 0 && updated[updated.length - 1].role === 'assistant' && !updated[updated.length - 1].content) {
            updated.pop();
          }
          return updated;
        });
      }
    } finally {
      setLoading(false);
      abortControllerRef.current = null;
    }
  };

  // 按 category 分组
  const groupedSkills = skills.reduce<Record<string, Skill[]>>((acc, skill) => {
    const cat = skill.category;
    if (!acc[cat]) acc[cat] = [];
    acc[cat].push(skill);
    return acc;
  }, {});

  const categoryColors: Record<string, string> = {
    'Skill·长篇': '#1890ff',
    'Skill·短篇': '#52c41a',
    'Skill·润色': '#faad14',
    'Skill·工具': '#722ed1',
  };

  if (selectedSkill) {
    return (
      <div style={{ height: 'calc(100vh - 120px)', display: 'flex', flexDirection: 'column', padding: '0 16px' }}>
        {/* 顶部栏 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 0', borderBottom: '1px solid #f0f0f0' }}>
          <Button size="small" onClick={() => { setSelectedSkill(null); setMessages([]); }}>← 返回</Button>
          <ThunderboltOutlined style={{ color: '#1890ff' }} />
          <Text strong>{selectedSkill.template_name}</Text>
          <Tag color={categoryColors[selectedSkill.category] || '#default'} style={{ marginLeft: 4 }}>{selectedSkill.category}</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>{selectedSkill.description}</Text>
        </div>

        {/* 消息区域 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '16px 0' }}>
          {messages.length === 0 && (
            <div style={{ textAlign: 'center', padding: '60px 20px', color: '#999' }}>
              <RobotOutlined style={{ fontSize: 48, marginBottom: 16 }} />
              <div style={{ fontSize: 16, marginBottom: 8 }}>{'已选择「'}{selectedSkill.template_name}{'」'}</div>
              <div>输入你的需求开始对话，或直接使用触发词：{selectedSkill.triggers.join('、')}</div>
            </div>
          )}
          {messages.map((msg, idx) => (
            <div key={idx} style={{
              display: 'flex', gap: 12, marginBottom: 16,
              flexDirection: msg.role === 'user' ? 'row-reverse' : 'row',
            }}>
              <div style={{
                width: 36, height: 36, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: msg.role === 'user' ? '#1890ff' : '#f0f0f0', color: msg.role === 'user' ? '#fff' : '#333',
                flexShrink: 0,
              }}>
                {msg.role === 'user' ? <UserOutlined /> : <RobotOutlined />}
              </div>
              <div style={{
                maxWidth: '75%', padding: '10px 16px', borderRadius: 12,
                background: msg.role === 'user' ? '#1890ff' : '#f5f5f5',
                color: msg.role === 'user' ? '#fff' : '#333',
              }}>
                {msg.role === 'assistant' ? (
                  <div className="markdown-body" style={{ fontSize: 14, lineHeight: 1.7 }}>
                    <MarkdownRender content={msg.content || '...'} />
                  </div>
                ) : (
                  <div style={{ fontSize: 14, whiteSpace: 'pre-wrap' }}>{msg.content}</div>
                )}
              </div>
            </div>
          ))}
          {loading && messages[messages.length - 1]?.content === '' && (
            <div style={{ textAlign: 'center', color: '#999', padding: 8 }}><Spin size="small" /> 思考中...</div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* 输入区域 */}
        <div style={{ padding: '12px 0', borderTop: '1px solid #f0f0f0', display: 'flex', gap: 8 }}>
          <TextArea
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            onPressEnter={e => { if (!e.shiftKey) { e.preventDefault(); handleSend(); } }}
            placeholder="输入你的需求..."
            autoSize={{ minRows: 1, maxRows: 4 }}
            disabled={loading}
          />
          <Button type="primary" icon={<SendOutlined />} onClick={handleSend} loading={loading} />
        </div>
      </div>
    );
  }

  // Skill 选择页
  return (
    <div style={{ padding: 24 }}>
      <Title level={4}><ThunderboltOutlined /> Skill 工具箱</Title>
      <Paragraph type="secondary">选择一个 Skill 开始创作对话。每个 Skill 都有专业的写作工作流和知识库。</Paragraph>
      
      {skillsLoading ? <Spin /> : (
        <Collapse
          defaultActiveKey={Object.keys(groupedSkills)}
          items={Object.entries(groupedSkills).map(([category, items]) => ({
            key: category,
            label: (
              <span>
                <Tag color={categoryColors[category] || '#default'}>{category}</Tag>
                {items.length} 个 Skill
              </span>
            ),
            children: (
              <List
                grid={{ gutter: 16, xs: 1, sm: 2, md: 3, lg: 3 }}
                dataSource={items}
                renderItem={(skill) => (
                  <List.Item>
                    <Card
                      hoverable
                      onClick={() => handleSkillSelect(skill)}
                      style={{ cursor: 'pointer', height: '100%' }}
                    >
                      <Card.Meta
                        title={<span><ThunderboltOutlined style={{ color: '#1890ff' }} /> {skill.template_name}</span>}
                        description={
                          <div>
                            <Paragraph ellipsis={{ rows: 2 }} style={{ marginBottom: 8 }}>{skill.description}</Paragraph>
                            <Space wrap>
                              {skill.triggers.map(t => (
                                <Tag key={t} style={{ fontSize: 11 }}>{t}</Tag>
                              ))}
                            </Space>
                          </div>
                        }
                      />
                    </Card>
                  </List.Item>
                )}
              />
            ),
          }))}
        />
      )}
    </div>
  );
};

export default SkillChat;
