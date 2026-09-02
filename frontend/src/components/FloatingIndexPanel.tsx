import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Drawer, Input, List, Typography, Empty, Tag, theme } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import type { Chapter } from '../types';

const { Link } = Typography;

interface GroupedChapters {
  outlineId: string | null;
  outlineTitle: string;
  chapters: Chapter[];
}

interface FloatingIndexPanelProps {
  visible: boolean;
  onClose: () => void;
  groupedChapters: GroupedChapters[];
  onChapterSelect: (chapterId: string) => void;
}

export default function FloatingIndexPanel({
  visible,
  onClose,
  groupedChapters,
  onChapterSelect,
}: FloatingIndexPanelProps) {
  const { token } = theme.useToken();
  const { t } = useTranslation();
  const [searchTerm, setSearchTerm] = useState('');

  const filteredGroups = useMemo(() => {
    if (!searchTerm) {
      return groupedChapters;
    }
    return groupedChapters
      .map(group => {
        const filteredChapters = group.chapters.filter(chapter =>
          chapter.title.toLowerCase().includes(searchTerm.toLowerCase())
        );
        return { ...group, chapters: filteredChapters };
      })
      .filter(group => group.chapters.length > 0);
  }, [searchTerm, groupedChapters]);

  const handleChapterClick = (chapterId: string) => {
    onChapterSelect(chapterId);
    onClose();
  };

  return (
    <Drawer
      title={t('indexPanel.title')}
      placement="right"
      onClose={onClose}
      open={visible}
      width={320}
      styles={{
        body: { padding: 0 },
      }}
    >
      <div style={{ padding: '16px', borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
        <Input
          placeholder={t('indexPanel.searchPlaceholder')}
          prefix={<SearchOutlined />}
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          allowClear
        />
      </div>

      {filteredGroups.length > 0 ? (
        <List
          dataSource={filteredGroups}
          renderItem={group => (
            <List.Item style={{ padding: '0 16px', flexDirection: 'column', alignItems: 'flex-start' }}>
              <div style={{ padding: '12px 0', fontWeight: 'bold' }}>
                <Tag color={group.outlineId ? 'blue' : 'default'}>
                  {group.outlineTitle}
                </Tag>
              </div>
              <List
                size="small"
                dataSource={group.chapters}
                renderItem={chapter => (
                  <List.Item style={{ paddingLeft: 16, borderBlockStart: 'none' }}>
                    <Link onClick={() => handleChapterClick(chapter.id)}>
                      {t('indexPanel.chapterItem', { number: chapter.chapter_number, title: chapter.title })}
                    </Link>
                  </List.Item>
                )}
                split={false}
              />
            </List.Item>
          )}
          style={{ height: 'calc(100vh - 120px)', overflowY: 'auto' }}
        />
      ) : (
        <Empty description={t('indexPanel.noMatch')} style={{ marginTop: 48 }} />
      )}
    </Drawer>
  );
}