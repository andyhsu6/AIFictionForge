import { useEffect, useState } from 'react';
import { FloatButton, Grid } from 'antd';
import { FileTextOutlined } from '@ant-design/icons';
import ChangelogModal from './ChangelogModal';

const { useBreakpoint } = Grid;

interface ChangelogFloatingButtonProps {
  defaultVisible?: boolean;
  onClose?: () => void;
}

export default function ChangelogFloatingButton({ defaultVisible = false, onClose }: ChangelogFloatingButtonProps) {
  const [showChangelog, setShowChangelog] = useState(defaultVisible);
  const screens = useBreakpoint();
  const isMobile = !screens.md;

  useEffect(() => {
    setShowChangelog(defaultVisible);
  }, [defaultVisible]);

  return (
    <>
      <FloatButton
        icon={<FileTextOutlined />}
        type="primary"
        tooltip="查看更新日志"
        style={{
          right: 24,
          bottom: 100,
          ...(isMobile ? {} : {
            zIndex: 999,
          }),
        }}
        onClick={() => setShowChangelog(true)}
      />

      <ChangelogModal
        visible={showChangelog}
        onClose={() => {
          setShowChangelog(false);
          onClose?.();
        }}
      />
    </>
  );
}
