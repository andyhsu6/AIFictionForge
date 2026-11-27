import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CharacterCard } from './CharacterCard';
import { CHARACTER_CARD_HEIGHT } from './CardStyles';
import i18n from '../i18n';
import type { Character } from '../types';

const baseCharacter: Character = {
  id: 'char-1',
  project_id: 'proj-1',
  name: '测试角色甲',
  is_organization: false,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

describe('CharacterCard uniform height', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('zh');
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('applies fixed uniform height and full width for minimal and full character cards', () => {
    const minimalCharacter: Character = {
      ...baseCharacter,
      id: 'char-minimal',
      name: '极简角色',
    };

    const fullCharacter: Character = {
      ...baseCharacter,
      id: 'char-full',
      name: '满字段测试角色名称超长省略验证',
      role_type: 'protagonist',
      age: '24',
      gender: '男',
      personality: '冷静果断，心思缜密，重情重义，善于观察局势变化',
      appearance: '身姿挺拔，眉目疏朗，常着深青长衫，腰佩玉珏',
      relationships: '测试角色乙（师兄）、测试角色丙（同门）',
      background: '自幼入山门修习，经历数次历练后逐步成长为宗门中流砥柱，背负重要使命。',
      profile_facts: [
        {
          field: 'personality',
          claim: '冷静果断',
          quote: '他神色如常，未露半分慌乱。',
          chapter_number: 1,
          inferred: false,
        },
      ],
    };

    const { container: minimalContainer } = render(
      <CharacterCard character={minimalCharacter} onEdit={vi.fn()} onDelete={vi.fn()} onExport={vi.fn()} />
    );
    const minimalCard = minimalContainer.querySelector('.ant-card') as HTMLElement;
    const minimalBody = minimalContainer.querySelector('.ant-card-body') as HTMLElement;
    const minimalActions = minimalContainer.querySelector('.ant-card-actions') as HTMLElement;

    expect(minimalCard.style.height).toBe(`${CHARACTER_CARD_HEIGHT}px`);
    expect(minimalCard.style.width).toBe('100%');
    expect(minimalBody.style.flex).toContain('1');
    expect(minimalBody.style.minHeight).toBe('0');
    expect(minimalActions.style.flexShrink).toBe('0');
    expect(screen.queryByRole('button', { name: '查看档案依据' })).toBeNull();

    const { container: fullContainer } = render(
      <CharacterCard character={fullCharacter} onEdit={vi.fn()} onDelete={vi.fn()} onExport={vi.fn()} />
    );
    const fullCard = fullContainer.querySelector('.ant-card') as HTMLElement;
    const fullBody = fullContainer.querySelector('.ant-card-body') as HTMLElement;
    const fullActions = fullContainer.querySelector('.ant-card-actions') as HTMLElement;
    const sourceBtn = screen.getByRole('button', { name: '查看档案依据' });

    expect(fullCard.style.height).toBe(`${CHARACTER_CARD_HEIGHT}px`);
    expect(fullCard.style.width).toBe('100%');
    expect(fullBody.style.flex).toContain('1');
    expect(fullBody.style.minHeight).toBe('0');
    expect(fullActions.style.flexShrink).toBe('0');
    expect(sourceBtn.style.height).toBe('22px');
  });

  it('applies fixed uniform height for organization cards with string or array members', () => {
    const orgWithStringMembers: Character = {
      ...baseCharacter,
      id: 'org-1',
      name: '测试组织甲',
      is_organization: true,
      organization_type: '宗门',
      power_level: 85,
      location: '东海群岛主峰',
      color: '玄青',
      motto: '守正笃实，久久为功',
      organization_purpose: '维护区域秩序与传承典籍',
      organization_members: '成员甲、成员乙、成员丙、成员丁',
      background: '创立于数百年前的古老宗门，门规森严，弟子遍布各地。',
    };

    const orgWithArrayMembers: Character = {
      ...orgWithStringMembers,
      id: 'org-2',
      name: '测试组织乙',
      organization_members: ['成员A', '成员B'] as unknown as string,
    };

    const { container: c1 } = render(
      <CharacterCard character={orgWithStringMembers} onDelete={vi.fn()} />
    );
    const card1 = c1.querySelector('.ant-card') as HTMLElement;
    expect(card1.style.height).toBe(`${CHARACTER_CARD_HEIGHT}px`);
    expect(card1.style.width).toBe('100%');
    expect(screen.getByText('成员甲、成员乙、成员丙、成员丁')).toBeInTheDocument();

    const { container: c2 } = render(
      <CharacterCard character={orgWithArrayMembers} onDelete={vi.fn()} />
    );
    const card2 = c2.querySelector('.ant-card') as HTMLElement;
    expect(card2.style.height).toBe(`${CHARACTER_CARD_HEIGHT}px`);
    expect(screen.getByText('["成员A","成员B"]')).toBeInTheDocument();
  });
});
