import { Modal, Empty, Spin, Button, Space, Typography } from 'antd';
import { useState, useEffect } from 'react';
import { FileTextOutlined, ReloadOutlined } from '@ant-design/icons';
import { fetchChangelog, type LocalChangelogEntry } from '../services/changelogService';

const { Text } = Typography;

interface ChangelogModalProps {
  visible: boolean;
  onClose: () => void;
}

export default function ChangelogModal({ visible, onClose }: ChangelogModalProps) {
  const [entries, setEntries] = useState<LocalChangelogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadChangelog = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchChangelog();
      setEntries(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取更新日志失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (visible) {
      loadChangelog();
    }
  }, [visible]);

  return (
    <Modal
      title={
        <Space>
          <FileTextOutlined />
          <span>更新日志</span>
          <Button
            type="text"
            size="small"
            icon={<ReloadOutlined />}
            onClick={loadChangelog}
            loading={loading}
            title="刷新"
          />
        </Space>
      }
      open={visible}
      onCancel={onClose}
      footer={null}
      width={800}
      centered
      styles={{
        body: {
          maxHeight: '70vh',
          overflowY: 'auto',
          padding: '24px',
        },
      }}
    >
      {error && (
        <div style={{
          padding: '16px',
          marginBottom: '16px',
          background: 'var(--color-error-bg)',
          border: '1px solid var(--color-error-border)',
          borderRadius: '4px',
          color: 'var(--color-error)',
        }}>
          {error}
        </div>
      )}

      {loading && entries.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <Spin size="large" tip="加载更新日志中..." />
        </div>
      ) : entries.length === 0 ? (
        <Empty description="暂无更新日志" />
      ) : (
        <div>
          {entries.map(entry => (
            <div
              key={entry.id}
              style={{
                whiteSpace: 'pre-wrap',
                fontSize: '14px',
                lineHeight: '1.7',
                color: 'var(--color-text-primary)',
              }}
            >
              {entry.message}
            </div>
          ))}
        </div>
      )}

      <div style={{
        marginTop: '24px',
        padding: '12px',
        background: 'var(--color-info-bg)',
        borderRadius: '4px',
        border: '1px solid var(--color-info-border)',
        fontSize: '13px',
        color: 'var(--color-primary)',
      }}>
        <Text>💡 更新日志来自本地 CHANGELOG.md 文件</Text>
      </div>
    </Modal>
  );
}
