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

  it('triggers onUploadCover from ready cover face and resets flipped state after upload', async () => {
    const onUploadCover = vi.fn().mockResolvedValue(true);
    const { container } = renderBookshelf([
      {
        ...baseProject,
        cover_status: 'ready',
        cover_image_url: '/generated-assets/covers/u1/old.png',
      },
    ], onUploadCover);

    const uploadBtn = screen.getByRole('button', { name: /上传封面/ });
    fireEvent.click(uploadBtn);

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(fileInput).not.toBeNull();

    const pngFile = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'custom.png', { type: 'image/png' });
    fireEvent.change(fileInput, { target: { files: [pngFile] } });

    await waitFor(() => {
      expect(onUploadCover).toHaveBeenCalledTimes(1);
    });
    expect(onUploadCover).toHaveBeenCalledWith(expect.objectContaining({ id: 'proj-1' }), pngFile);
    expect(screen.getByRole('button', { name: /切换回详情/ })).toBeInTheDocument();
  });

  it('also flips back to cover face after uploading from flipped no-cover panel', async () => {
    const onUploadCover = vi.fn().mockResolvedValue(true);
    const project: Project = { ...baseProject, cover_status: 'none' };
    const { container, rerender } = renderBookshelf([project], onUploadCover);

    fireEvent.click(screen.getByRole('button', { name: /封面操作/ }));
    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const jpgFile = new File([new Uint8Array([0xff, 0xd8, 0xff])], 'custom.jpg', { type: 'image/jpeg' });
    fireEvent.change(fileInput, { target: { files: [jpgFile] } });

    await waitFor(() => {
      expect(onUploadCover).toHaveBeenCalledTimes(1);
    });

    rerender(
      <ThemeProvider>
        <BookshelfPage
          isMobile={false}
          loading={false}
          projects={[{ ...project, cover_status: 'ready', cover_image_url: '/generated-assets/covers/u1/new.jpg' }]}
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

    expect(screen.getByRole('button', { name: /切换回详情/ })).toBeInTheDocument();
  });

  it('rejects unsupported file type and oversized file before calling onUploadCover', async () => {
    const onUploadCover = vi.fn();
    const { container } = renderBookshelf([{ ...baseProject, cover_status: 'none' }], onUploadCover);

    fireEvent.click(screen.getByRole('button', { name: /封面操作/ }));
    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;

    const txtFile = new File(['hello'], 'not-image.txt', { type: 'text/plain' });
    fireEvent.change(fileInput, { target: { files: [txtFile] } });
    expect(onUploadCover).not.toHaveBeenCalled();
    expect(await screen.findByText('仅支持 PNG、JPG 或 WebP 格式的图片')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /上传封面/ }));
    const bigFile = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'big.png', { type: 'image/png' });
    Object.defineProperty(bigFile, 'size', { value: 10 * 1024 * 1024 + 1 });
    fireEvent.change(fileInput, { target: { files: [bigFile] } });
    expect(onUploadCover).not.toHaveBeenCalled();
    expect(await screen.findByText('封面图片大小不能超过 10MB')).toBeInTheDocument();
  });
});
