import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import BookshelfPage from './BookshelfPage';
import { ThemeProvider } from '../theme/ThemeProvider';
import i18n from '../i18n';
import type { Project } from '../types';

const baseProject: Project = {
  id: 'proj-1',
  title: '测试书架作品',
  description: '测试描述',
  genre: '奇幻',
  current_words: 1000,
  target_words: 10000,
  status: 'writing',
  wizard_status: 'completed',
  outline_mode: 'one-to-many',
  cover_status: 'none',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

function renderBookshelf(
  projects: Project[],
  onUploadCover: (project: Project, file: File) => boolean | void | Promise<boolean | void> = vi.fn().mockResolvedValue(true),
) {
  return render(
    <ThemeProvider>
      <BookshelfPage
        isMobile={false}
        loading={false}
        projects={projects}
        showApiTip={false}
        setShowApiTip={vi.fn()}
        exportableProjectsCount={0}
        onOpenImportModal={vi.fn()}
        onOpenExportModal={vi.fn()}
        onGoSettings={vi.fn()}
        onStartWizard={vi.fn()}
        onOpenInspiration={vi.fn()}
        onEnterProject={vi.fn()}
        onDeleteProject={vi.fn()}
        onGenerateCover={vi.fn()}
        onUploadCover={onUploadCover}
        onDownloadCover={vi.fn()}
        formatWordCount={(c) => `${c}`}
        getProgress={() => 10}
        getProgressColor={() => '#1890ff'}
        getDisplayStatus={() => '连载中'}
        getStatusTag={() => null}
        formatDate={() => '2026-01-01'}
      />
    </ThemeProvider>,
  );
}

describe('BookshelfPage custom cover upload', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('zh');
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('shows upload cover button alongside generate cover when flipped with no cover', () => {
    renderBookshelf([{ ...baseProject, cover_status: 'none' }]);

    fireEvent.click(screen.getByRole('button', { name: /封面操作/ }));

    expect(screen.getByRole('button', { name: /生成封面/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /上传封面/ })).toBeInTheDocument();
  });

  it('shows upload cover button alongside regenerate when flipped after cover generation failed', () => {
    renderBookshelf([
      {
        ...baseProject,
        cover_status: 'failed',
        cover_error: 'upstream timeout',
      },
    ]);

    fireEvent.click(screen.getByRole('button', { name: /封面操作/ }));

    expect(screen.getByRole('button', { name: /重新生成/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /上传封面/ })).toBeInTheDocument();
  });

  it('keeps failed cover panel visible while uploading instead of flashing empty cover state', async () => {
    let resolveUpload!: (ok: boolean) => void;
    const onUploadCover = vi.fn().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolveUpload = resolve; }),
    );
    const { container } = renderBookshelf(
      [
        {
          ...baseProject,
          cover_status: 'failed',
          cover_error: 'upstream timeout',
        },
      ],
      onUploadCover,
    );

    fireEvent.click(screen.getByRole('button', { name: /封面操作/ }));
    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const pngFile = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'custom.png', { type: 'image/png' });
    fireEvent.change(fileInput, { target: { files: [pngFile] } });

    await waitFor(() => {
      expect(onUploadCover).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByText('封面生成失败')).toBeInTheDocument();
    expect(screen.queryByText('当前暂无可用封面')).toBeNull();
    expect(screen.getByRole('button', { name: /上传中/ })).toBeInTheDocument();

    resolveUpload(false);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /上传封面/ })).toBeInTheDocument();
    });
    expect(screen.getByText('封面生成失败')).toBeInTheDocument();
  });

  it('triggers onUploadCover from ready cover face and allows re-selecting the same file', async () => {
    const onUploadCover = vi.fn().mockResolvedValue(true);
    const { container } = renderBookshelf(
      [
        {
          ...baseProject,
          cover_status: 'ready',
          cover_image_url: '/generated-assets/covers/u1/old.png',
        },
      ],
      onUploadCover,
    );

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const pngFile = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'custom.png', { type: 'image/png' });

    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));
    fireEvent.change(fileInput, { target: { files: [pngFile] } });
    await waitFor(() => {
      expect(onUploadCover).toHaveBeenCalledTimes(1);
    });
    expect(fileInput.value).toBe('');

    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));
    fireEvent.change(fileInput, { target: { files: [pngFile] } });
    await waitFor(() => {
      expect(onUploadCover).toHaveBeenCalledTimes(2);
    });
    expect(screen.getByTestId('cover-hover-trigger-proj-1')).toBeInTheDocument();
  });

  it('flips back to cover face after upload succeeds, and stays flipped if upload returns false', async () => {
    const onUploadCover = vi
      .fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    const project: Project = { ...baseProject, cover_status: 'none' };
    const { container, rerender } = renderBookshelf([project], onUploadCover);

    fireEvent.click(screen.getByRole('button', { name: /封面操作/ }));
    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const webpFile = new File([new Uint8Array(24)], 'custom.webp', { type: '' });
    fireEvent.change(fileInput, { target: { files: [webpFile] } });

    await waitFor(() => {
      expect(onUploadCover).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByText('当前暂无可用封面')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));
    fireEvent.change(fileInput, { target: { files: [webpFile] } });

    await waitFor(() => {
      expect(onUploadCover).toHaveBeenCalledTimes(2);
    });

    rerender(
      <ThemeProvider>
        <BookshelfPage
          isMobile={false}
          loading={false}
          projects={[{ ...project, cover_status: 'ready', cover_image_url: '/generated-assets/covers/u1/new.webp' }]}
          showApiTip={false}
          setShowApiTip={vi.fn()}
          exportableProjectsCount={0}
          onOpenImportModal={vi.fn()}
          onOpenExportModal={vi.fn()}
          onGoSettings={vi.fn()}
          onStartWizard={vi.fn()}
          onOpenInspiration={vi.fn()}
          onEnterProject={vi.fn()}
          onDeleteProject={vi.fn()}
          onGenerateCover={vi.fn()}
          onUploadCover={onUploadCover}
          onDownloadCover={vi.fn()}
          formatWordCount={(c) => `${c}`}
          getProgress={() => 10}
          getProgressColor={() => '#1890ff'}
          getDisplayStatus={() => '连载中'}
          getStatusTag={() => null}
          formatDate={() => '2026-01-01'}
        />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('cover-hover-trigger-proj-1')).toBeInTheDocument();
  });

  it('rejects empty, unsupported, corrupted, and oversized files before calling onUploadCover', async () => {
    const onUploadCover = vi.fn();
    const createBitmapMock = vi.fn().mockRejectedValue(new Error('corrupt image'));
    vi.stubGlobal('createImageBitmap', createBitmapMock);

    const { container } = renderBookshelf([{ ...baseProject, cover_status: 'none' }], onUploadCover);
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;

    fireEvent.click(screen.getByRole('button', { name: /封面操作/ }));

    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));
    const txtFile = new File(['hello'], 'not-image.txt', { type: 'text/plain' });
    fireEvent.change(fileInput, { target: { files: [txtFile] } });
    expect(onUploadCover).not.toHaveBeenCalled();
    expect(await screen.findByText('仅支持 PNG、JPG 或 WebP 格式的图片')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));
    const emptyPng = new File([], 'empty.png', { type: 'image/png' });
    fireEvent.change(fileInput, { target: { files: [emptyPng] } });
    expect(onUploadCover).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));
    const corruptPng = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'corrupt.png', { type: 'image/png' });
    fireEvent.change(fileInput, { target: { files: [corruptPng] } });
    await waitFor(() => {
      expect(createBitmapMock).toHaveBeenCalledWith(corruptPng);
    });
    expect(onUploadCover).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));
    const bigFile = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'big.png', { type: 'image/png' });
    Object.defineProperty(bigFile, 'size', { value: 10 * 1024 * 1024 + 1 });
    fireEvent.change(fileInput, { target: { files: [bigFile] } });
    expect(onUploadCover).not.toHaveBeenCalled();
    expect(await screen.findByText('封面图片大小不能超过 10MB')).toBeInTheDocument();
  });

  it('renders transparent cover face bottom controls and stats box without opaque background or backdrop blur', () => {
    renderBookshelf([
      {
        ...baseProject,
        cover_status: 'ready',
        cover_image_url: '/generated-assets/covers/u1/cover.png',
      },
    ]);

    const progressLabel = screen.getByText('完成进度');
    const statsBox = progressLabel.parentElement?.parentElement as HTMLElement;
    expect(statsBox).not.toBeNull();
    expect(statsBox.style.background).toBe('transparent');
    expect(statsBox.style.backdropFilter || '').toBe('');
    expect(statsBox.style.boxShadow).toBe('');

    const downloadBtn = screen.getByRole('button', { name: /下载封面/ });
    const uploadBtn = screen.getByRole('button', { name: /上传封面/ });
    const regenBtn = screen.getByRole('button', { name: /重新生成/ });

    for (const el of [downloadBtn, uploadBtn, regenBtn]) {
      expect(el).not.toBeNull();
      expect(el.style.background).toBe('transparent');
      expect(el.style.backdropFilter || '').toBe('');
    }
  });

  it('hides switch button, title, and genre tags behind a hover dot on cover face and reveals them on hover', () => {
    renderBookshelf([
      {
        ...baseProject,
        cover_status: 'ready',
        cover_image_url: '/generated-assets/covers/u1/cover.png',
      },
    ]);

    const trigger = screen.getByTestId('cover-hover-trigger-proj-1');
    expect(trigger).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /切换回详情/ })).toBeNull();
    expect(screen.queryByText('测试书架作品')).toBeNull();
    expect(screen.queryByText('奇幻')).toBeNull();

    fireEvent.mouseEnter(trigger);
    expect(screen.getByRole('button', { name: /切换回详情/ })).toBeInTheDocument();
    expect(screen.getByText('测试书架作品')).toBeInTheDocument();
    expect(screen.getByText('奇幻')).toBeInTheDocument();

    fireEvent.mouseLeave(trigger);
    expect(screen.queryByRole('button', { name: /切换回详情/ })).toBeNull();
    expect(screen.queryByText('测试书架作品')).toBeNull();

    fireEvent.click(trigger);
    expect(screen.getByRole('button', { name: /切换回详情/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /切换回详情/ }));
    expect(screen.getByRole('button', { name: /查看封面/ })).toBeInTheDocument();
  });
});
