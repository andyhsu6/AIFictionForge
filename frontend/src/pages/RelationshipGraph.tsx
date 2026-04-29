import { useState, useEffect, useCallback, useMemo, type CSSProperties } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Card, Tag, Button, Space, message, Typography, theme } from 'antd';
import {
  ArrowLeftOutlined,
  ApartmentOutlined,
  UserOutlined,
  TeamOutlined,
  TrophyOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import dagre from 'dagre';
import {
  ReactFlow,
  Background,
  Controls,
  useNodesState,
  useEdgesState,
  BackgroundVariant,
  MarkerType,
} from '@xyflow/react';
import type { Node, Edge } from '@xyflow/react';
import '@xyflow/react/dist/style.css';

const { Text } = Typography;

interface GraphNode {
  id: string;
  name: string;
  type: string;
  role_type: string;
  avatar: string | null;
}

interface GraphLink {
  source: string;
  target: string;
  relationship: string;
  intimacy: number;
  status: string;
}

interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

interface RelationshipType {
  id: number;
  name: string;
  category: string;
  reverse_name: string;
  intimacy_range: string;
  icon: string;
  description: string;
  created_at: string;
}

interface CharacterDetail {
  id: string;
  project_id: string;
  name: string;
  age: string;
  gender: string;
  is_organization: boolean;
  role_type: string;
  personality: string;
  background: string;
  appearance: string;
  organization_type: string;
  organization_purpose: string;
  organization_members: string;
  traits: string;
  avatar_url: string;
  power_level: number;
  location: string;
  motto: string;
  color: string;
  main_career_id?: string;
  main_career_stage?: number;
  sub_careers?: Array<{ career_id: string; stage?: number }> | string | null;
}

interface CareerItem {
  id: string;
  name: string;
  type: 'main' | 'sub';
  max_stage: number;
}

interface CareerListResponse {
  main_careers?: CareerItem[];
  sub_careers?: CareerItem[];
}

interface CharacterListResponse {
  items?: CharacterDetail[];
}

const GROUP_MAIN_CAREER_NODE_ID = '__career_group_main__';
const GROUP_SUB_CAREER_NODE_ID = '__career_group_sub__';

type EdgeColorPreset = 'primary' | 'warning' | 'info' | 'textTertiary' | 'error' | 'success';

const EDGE_CATEGORY_META: Record<string, { label: string; colorPreset: EdgeColorPreset; order: number }> = {
  organization: { label: '组织成员', colorPreset: 'primary', order: 1 },
  career_main: { label: '主职业关联', colorPreset: 'warning', order: 2 },
  career_sub: { label: '副职业关联', colorPreset: 'info', order: 3 },
  career_group: { label: '职业分类', colorPreset: 'textTertiary', order: 4 },
  family: { label: '亲属关系', colorPreset: 'warning', order: 5 },
  hostile: { label: '敌对关系', colorPreset: 'error', order: 6 },
  professional: { label: '职业关系', colorPreset: 'info', order: 7 },
  social: { label: '社交关系', colorPreset: 'success', order: 8 },
  default: { label: '其他关系', colorPreset: 'textTertiary', order: 99 },
};

const getEdgeCategory = (edge: Edge) =>
  typeof edge.data?.category === 'string' ? edge.data.category : 'default';

const getEdgeCategoryMeta = (category: string) =>
  EDGE_CATEGORY_META[category] || {
    label: `${category}关系`,
    colorPreset: 'textTertiary' as const,
    order: 999,
  };

const resolveEdgePresetColor = (
  colorPreset: EdgeColorPreset,
  token: {
    colorPrimary: string;
    colorWarning: string;
    colorInfo: string;
    colorTextTertiary: string;
    colorError: string;
    colorSuccess: string;
  },
) => {
  const colorMap: Record<EdgeColorPreset, string> = {
    primary: token.colorPrimary,
    warning: token.colorWarning,
    info: token.colorInfo,
    textTertiary: token.colorTextTertiary,
    error: token.colorError,
    success: token.colorSuccess,
  };

  return colorMap[colorPreset];
};

const clampTextStyle = (rows: number): CSSProperties => ({
  margin: '4px 0 0',
  color: 'var(--ant-color-text-secondary)',
  fontSize: 14,
  lineHeight: '22px',
  display: '-webkit-box',
  WebkitBoxOrient: 'vertical',
  WebkitLineClamp: rows,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  wordBreak: 'break-word',
});

const getNodeSize = (node: Node) => {
  const width =
    typeof node.style?.width === 'number'
      ? node.style.width
      : Number(node.style?.width ?? 140) || 140;
  const height =
    typeof node.style?.height === 'number'
      ? node.style.height
      : Number(node.style?.height ?? 60) || 60;

  return { width, height };
};

const MAIN_GRAPH_FIXED_X_GAP = 220;
const MAIN_GRAPH_FIXED_Y_GAP = 180;
const MAIN_GRAPH_MAX_PER_ROW = 6;
const MAIN_GRAPH_GROUP_Y_GAP = 140;

const layoutNodesInWrappedRows = (
  rowNodes: Node[],
  startX: number,
  startY: number,
  maxPerRow: number,
  columnGap: number,
  rowGap: number,
): Node[] => {
  if (rowNodes.length === 0) {
    return [];
  }

  const sorted = [...rowNodes].sort((a, b) => a.position.x - b.position.x);

  return sorted.map((node, index) => {
    const col = index % maxPerRow;
    const row = Math.floor(index / maxPerRow);
    return {
      ...node,
      position: {
        ...node.position,
        x: startX + col * columnGap,
        y: startY + row * rowGap,
      },
    };
  });
};

// 使用 dagre 进行自动布局，并支持分组排版策略
const getLayoutedElements = (nodes: Node[], edges: Edge[]) => {
  const dagreGraph = new dagre.graphlib.Graph();
  dagreGraph.setDefaultEdgeLabel(() => ({}));
  // 增大节点间距，使用更合理的排版
  dagreGraph.setGraph({ rankdir: 'TB', nodesep: 160, ranksep: 180, edgesep: 80, marginx: 40, marginy: 40 });

  // 1. 拆分职业节点和其他节点
  const careerNodeIds = new Set(
    nodes.filter((n) => n.id.startsWith('career-') || n.id.startsWith('__career_group')).map((n) => n.id)
  );

  const layoutNodes = nodes.filter((n) => !careerNodeIds.has(n.id));
  const careerNodes = nodes.filter((n) => careerNodeIds.has(n.id));

  // 2. 配置主图谱 (组织 + 角色)
  layoutNodes.forEach((node) => {
    const { width, height } = getNodeSize(node);
    dagreGraph.setNode(node.id, { width, height });
  });

  // 使用虚拟根节点强制分层：第一排组织，第二排角色
  dagreGraph.setNode('__dummy_root', { width: 1, height: 1 });
  layoutNodes.forEach((node) => {
    if (node.data?.type === 'organization') {
      dagreGraph.setEdge('__dummy_root', node.id, { weight: 100, minlen: 1 });
    } else {
      // 角色统一放在第二排（minlen=2）
      dagreGraph.setEdge('__dummy_root', node.id, { weight: 1, minlen: 2 });
    }
  });

  // 添加常规连线（排除职业相关的连线参与 Dagre 布局，避免干扰主图谱结构）
  edges.forEach((edge) => {
    if (!careerNodeIds.has(edge.source) && !careerNodeIds.has(edge.target)) {
      dagreGraph.setEdge(edge.source, edge.target, {
        weight: edge.data?.layoutWeight ?? 1,
        minlen: 1
      });
    }
  });

  dagre.layout(dagreGraph);

  // 3. 应用 Dagre 布局结果，并执行“首元素对齐 + 每排最多6个自动换行”
  const layoutedNodes = layoutNodes.map((node) => {
    const nodeWithPosition = dagreGraph.node(node.id);
    const { width, height } = getNodeSize(node);

    return {
      ...node,
      position: {
        x: nodeWithPosition.x - width / 2,
        y: nodeWithPosition.y - height / 2,
      },
    };
  });

  const organizationNodes = layoutedNodes.filter((node) => node.data?.type === 'organization');
  const characterNodes = layoutedNodes.filter((node) => node.data?.type !== 'organization');

  const baseStartX = layoutedNodes.reduce(
    (min, node) => (node.position.x < min ? node.position.x : min),
    Infinity,
  );
  const alignedStartX = Number.isFinite(baseStartX) ? baseStartX : 0;

  const orgStartYRaw = organizationNodes.reduce(
    (min, node) => (node.position.y < min ? node.position.y : min),
    Infinity,
  );
  const orgStartY = Number.isFinite(orgStartYRaw) ? orgStartYRaw : 0;

  const orgRows = Math.ceil(organizationNodes.length / MAIN_GRAPH_MAX_PER_ROW);
  const orgMaxHeight = organizationNodes.reduce(
    (max, node) => Math.max(max, getNodeSize(node).height),
    0,
  );
  const organizationBottomY =
    orgStartY +
    Math.max(orgRows - 1, 0) * MAIN_GRAPH_FIXED_Y_GAP +
    orgMaxHeight;

  const characterStartYRaw = characterNodes.reduce(
    (min, node) => (node.position.y < min ? node.position.y : min),
    Infinity,
  );
  const characterStartYBase = Number.isFinite(characterStartYRaw)
    ? characterStartYRaw
    : organizationBottomY + MAIN_GRAPH_GROUP_Y_GAP;
  const characterStartY = Math.max(characterStartYBase, organizationBottomY + MAIN_GRAPH_GROUP_Y_GAP);

  const wrappedOrganizations = layoutNodesInWrappedRows(
    organizationNodes,
    alignedStartX,
    orgStartY,
    MAIN_GRAPH_MAX_PER_ROW,
    MAIN_GRAPH_FIXED_X_GAP,
    MAIN_GRAPH_FIXED_Y_GAP,
  );

  const wrappedCharacters = layoutNodesInWrappedRows(
    characterNodes,
    alignedStartX,
    characterStartY,
    MAIN_GRAPH_MAX_PER_ROW,
    MAIN_GRAPH_FIXED_X_GAP,
    MAIN_GRAPH_FIXED_Y_GAP,
  );

  const normalizedMap = new Map<string, Node>(
    [...wrappedOrganizations, ...wrappedCharacters].map((node) => [node.id, node]),
  );

  const normalizedLayoutedNodes = layoutedNodes.map((node) => normalizedMap.get(node.id) || node);

  const { minX, minY } = normalizedLayoutedNodes.reduce(
    (acc, node) => ({
      minX: Math.min(acc.minX, node.position.x),
      minY: Math.min(acc.minY, node.position.y),
    }),
    { minX: Infinity, minY: Infinity },
  );

  const safeMinX = Number.isFinite(minX) ? minX : 0;
  const safeMinY = Number.isFinite(minY) ? minY : 0;

  // 4. 在左侧独立排版职业节点
  const careerStartX = safeMinX - 460; // 在主图左侧留出足够空间
  let currentY = safeMinY;

  const placedCareerNodes: Node[] = [];
  const placeNode = (nodeId: string, xOffset = 0) => {
    const node = careerNodes.find((n) => n.id === nodeId);
    if (node) {
      placedCareerNodes.push({
        ...node,
        position: { x: careerStartX + xOffset, y: currentY },
      });
      const { height } = getNodeSize(node);
      currentY += height + 30; // 节点垂直间距
    }
  };

  // 依次排列：主职业分组 -> 主职业列表 -> 副职业分组 -> 副职业列表
  placeNode(GROUP_MAIN_CAREER_NODE_ID, -180);
  careerNodes.filter((n) => n.data?.type === 'career_main').forEach((n) => placeNode(n.id));
  
  currentY += 20; // 主副职业之间的额外间距
  
  placeNode(GROUP_SUB_CAREER_NODE_ID, -180);
  careerNodes.filter((n) => n.data?.type === 'career_sub').forEach((n) => placeNode(n.id));

  return { nodes: [...normalizedLayoutedNodes, ...placedCareerNodes], edges };
};

const safeParseStringArray = (raw: unknown): string[] => {
  if (!raw) return [];

  if (Array.isArray(raw)) {
    return raw.map((item) => String(item)).filter(Boolean);
  }

  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw) as unknown;
      if (Array.isArray(parsed)) {
        return parsed.map((item) => String(item)).filter(Boolean);
      }
    } catch {
      return raw
        .split(/[，,、]/)
        .map((item) => item.trim())
        .filter(Boolean);
    }
  }

  return [];
};

const safeParseSubCareers = (raw: CharacterDetail['sub_careers']) => {
  if (!raw) return [] as Array<{ career_id: string; stage?: number }>;

  if (Array.isArray(raw)) {
    return raw.filter((item) => item?.career_id);
  }

  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw) as Array<{ career_id: string; stage?: number }>;
      if (Array.isArray(parsed)) {
        return parsed.filter((item) => item?.career_id);
      }
    } catch {
      return [];
    }
  }

  return [];
};

const getCategoryColor = (
  relationshipName: string,
  isActive: boolean,
  relationshipTypes: RelationshipType[],
  token: {
    colorPrimary: string;
    colorWarning: string;
    colorInfo: string;
    colorTextTertiary: string;
    colorError: string;
    colorSuccess: string;
    colorBorder: string;
  },
) => {
  const inactiveColor = token.colorBorder;

  if (relationshipName.startsWith('组织成员·')) {
    return isActive ? token.colorPrimary : inactiveColor;
  }

  if (relationshipName.startsWith('主职业·')) {
    return isActive ? token.colorWarning : inactiveColor;
  }

  if (relationshipName.startsWith('副职业·')) {
    return isActive ? token.colorInfo : inactiveColor;
  }

  if (relationshipName.startsWith('职业分类·')) {
    return isActive ? token.colorTextTertiary : inactiveColor;
  }

  const relType = relationshipTypes.find((rt) => rt.name === relationshipName);
  const category = relType?.category || 'default';

  const categoryColors: Record<string, string> = {
    family: token.colorWarning,
    hostile: token.colorError,
    professional: token.colorInfo,
    social: token.colorSuccess,
    default: token.colorTextTertiary,
  };

  const activeColor = categoryColors[category] || categoryColors.default;
  return isActive ? activeColor : inactiveColor;
};

const getCharacterNodeStyle = (roleType: string): CSSProperties => {
  const roleColorMap: Record<string, string> = {
    protagonist: 'var(--ant-color-error)',
    antagonist: 'var(--ant-color-primary)',
    supporting: 'var(--ant-color-info)',
  };

  const baseColor = roleColorMap[roleType] || 'var(--ant-color-info)';

  return {
    width: 130,
    height: 130,
    border: `2px solid ${baseColor}`,
    borderRadius: '50%',
    background: `linear-gradient(135deg, var(--ant-color-bg-container), color-mix(in srgb, ${baseColor} 12%, var(--ant-color-bg-container)))`,
    boxShadow: `0 4px 16px color-mix(in srgb, ${baseColor} 25%, transparent)`,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 0,
    transition: 'all 0.3s ease',
  };
};

const getOrganizationNodeStyle = (): CSSProperties => ({
  width: 160,
  height: 90,
  border: '2px solid var(--ant-color-success)',
  borderRadius: 12,
  background: 'linear-gradient(135deg, var(--ant-color-bg-container), color-mix(in srgb, var(--ant-color-success) 12%, var(--ant-color-bg-container)))',
  boxShadow: '0 4px 16px color-mix(in srgb, var(--ant-color-success) 15%, transparent)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 0,
  transition: 'all 0.3s ease',
});

const getCareerNodeStyle = (type: 'main' | 'sub'): CSSProperties => {
  const color = type === 'main' ? 'var(--ant-color-warning)' : 'var(--ant-color-info)';

  return {
    width: 150,
    height: 72,
    border: `2px solid ${color}`,
    borderRadius: 12,
    background: `linear-gradient(135deg, var(--ant-color-bg-container), color-mix(in srgb, ${color} 12%, var(--ant-color-bg-container)))`,
    boxShadow: `0 4px 12px color-mix(in srgb, ${color} 20%, transparent)`,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 0,
    transition: 'all 0.3s ease',
  };
};

const getCareerGroupStyle = (type: 'main' | 'sub'): CSSProperties => {
  const color = type === 'main' ? 'var(--ant-color-warning)' : 'var(--ant-color-info)';

  return {
    width: 130,
    height: 52,
    border: `2px dashed ${color}`,
    borderRadius: 26,
    backgroundColor: 'var(--ant-color-bg-container)',
    boxShadow: `0 2px 8px color-mix(in srgb, ${color} 15%, transparent)`,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontWeight: 600,
    fontSize: 13,
    color,
    padding: 0,
  };
};

const InfoField = ({
  label,
  value,
  rows = 2,
}: {
  label: string;
  value?: string | null;
  rows?: number;
}) => {
  if (!value) return null;

  return (
    <div
      style={{
        marginBottom: 12,
        padding: '12px 14px',
        borderRadius: 12,
        background: 'var(--ant-color-fill-quaternary)',
        border: '1px solid var(--ant-color-border-secondary)',
        boxShadow: '0 2px 4px color-mix(in srgb, var(--ant-color-text) 6%, transparent)',
      }}
    >
      <Text strong style={{ fontSize: 14, color: 'var(--ant-color-text)' }}>
        {label}
      </Text>
      <div style={clampTextStyle(rows)}>{value}</div>
    </div>
  );
};

export default function RelationshipGraph() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) =>
    `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;

  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [, setLoading] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [nodeDetail, setNodeDetail] = useState<CharacterDetail | null>(null);
  const [, setDetailLoading] = useState(false);
  const [relationshipTypes, setRelationshipTypes] = useState<RelationshipType[]>([]);
  const [characterDetailMap, setCharacterDetailMap] = useState<Record<string, CharacterDetail>>({});
  const [mainCareers, setMainCareers] = useState<CareerItem[]>([]);
  const [subCareers, setSubCareers] = useState<CareerItem[]>([]);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [edgeVisibilityMap, setEdgeVisibilityMap] = useState<Record<string, boolean>>({});

  const careerNameMap = useMemo(() => {
    const map: Record<string, CareerItem> = {};
    [...mainCareers, ...subCareers].forEach((career) => {
      map[career.id] = career;
    });
    return map;
  }, [mainCareers, subCareers]);

  const edgeCategoryOptions = useMemo(() => {
    const counter = new Map<string, number>();

    edges.forEach((edge) => {
      const category = getEdgeCategory(edge);
      counter.set(category, (counter.get(category) || 0) + 1);
    });

    return Array.from(counter.entries())
      .map(([category, count]) => {
        const meta = getEdgeCategoryMeta(category);
        return {
          category,
          count,
          label: meta.label,
          color: resolveEdgePresetColor(meta.colorPreset, token),
          order: meta.order,
        };
      })
      .sort((a, b) => a.order - b.order || a.label.localeCompare(b.label, 'zh-CN'));
  }, [edges, token]);

  useEffect(() => {
    if (edgeCategoryOptions.length === 0) {
      return;
    }

    setEdgeVisibilityMap((prev) => {
      const next: Record<string, boolean> = {};
      edgeCategoryOptions.forEach((option) => {
        next[option.category] = prev[option.category] ?? true;
      });
      return next;
    });
  }, [edgeCategoryOptions]);

  const visibleEdges = useMemo(
    () => edges.filter((edge) => edgeVisibilityMap[getEdgeCategory(edge)] !== false),
    [edges, edgeVisibilityMap],
  );

  const toggleEdgeCategoryVisibility = (category: string) => {
    setEdgeVisibilityMap((prev) => ({
      ...prev,
      [category]: !(prev[category] ?? true),
    }));
  };

  useEffect(() => {
    if (projectId) {
      void loadRelationshipTypes();
    }
  }, [projectId]);

  const loadRelationshipTypes = async () => {
    try {
      const res = await axios.get('/api/relationships/types');
      setRelationshipTypes(res.data || []);
    } catch (error) {
      console.error('加载关系类型失败', error);
    }
  };

  const buildFlowEdge = useCallback(
    (
      edgeId: string,
      source: string,
      target: string,
      relationship: string,
      status: string,
      intimacy: number,
      opts?: {
        dashed?: boolean;
        animated?: boolean;
        layoutWeight?: number;
      },
    ): Edge => {
      const edgeColor = getCategoryColor(relationship, status === 'active', relationshipTypes, {
        colorPrimary: token.colorPrimary,
        colorWarning: token.colorWarning,
        colorInfo: token.colorInfo,
        colorTextTertiary: token.colorTextTertiary,
        colorError: token.colorError,
        colorSuccess: token.colorSuccess,
        colorBorder: token.colorBorder,
      });
      const isOrgMemberLink = relationship.startsWith('组织成员·');
      const isCareerMainLink = relationship.startsWith('主职业·');
      const isCareerSubLink = relationship.startsWith('副职业·');
      const isCareerClassLink = relationship.startsWith('职业分类·');

      return {
        id: edgeId,
        source,
        target,
        label: relationship,
        type: 'smoothstep',
        animated: opts?.animated,
        style: {
          stroke: edgeColor,
          strokeWidth: isCareerClassLink ? 1.5 : 2,
          strokeDasharray: opts?.dashed || isOrgMemberLink || isCareerSubLink ? '6 3' : undefined,
          opacity: isCareerClassLink ? 0.5 : (isCareerMainLink || isCareerSubLink ? 0.6 : 1),
        },
        labelStyle: {
          fill: token.colorTextSecondary,
          fontSize: 10,
          fontWeight: isCareerMainLink || isCareerSubLink ? 600 : 500,
        },
        labelBgStyle: {
          fill: token.colorBgContainer,
          fillOpacity: 0.9,
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: edgeColor,
        },
        data: {
          intimacy,
          status,
          layoutWeight: opts?.layoutWeight ?? 1,
          category: isOrgMemberLink
            ? 'organization'
            : isCareerMainLink
              ? 'career_main'
              : isCareerSubLink
                ? 'career_sub'
                : isCareerClassLink
                  ? 'career_group'
                  : relationshipTypes.find((rt) => rt.name === relationship)?.category || 'social',
        },
      };
    },
    [
      relationshipTypes,
      token.colorBgContainer,
      token.colorBorder,
      token.colorError,
      token.colorInfo,
      token.colorPrimary,
      token.colorSuccess,
      token.colorTextSecondary,
      token.colorTextTertiary,
      token.colorWarning,
    ],
  );

  const loadGraphData = useCallback(async () => {
    if (!projectId || relationshipTypes.length === 0) return;

    setLoading(true);
    try {
      const [graphRes, charactersRes, careersRes] = await Promise.all([
        axios.get(`/api/relationships/graph/${projectId}`),
        axios.get('/api/characters', { params: { project_id: projectId } }),
        axios.get('/api/careers', { params: { project_id: projectId } }),
      ]);

      const data = graphRes.data as GraphData;
      const characters = (charactersRes.data as CharacterListResponse)?.items || [];
      const careersData = (careersRes.data as CareerListResponse) || {};

      setMainCareers(careersData.main_careers || []);
      setSubCareers(careersData.sub_careers || []);

      const detailMap: Record<string, CharacterDetail> = {};
      characters.forEach((item) => {
        detailMap[item.id] = item;
      });
      setCharacterDetailMap(detailMap);

      const baseNodes: Node[] = data.nodes.map((node) => {
        const style = node.type === 'organization' ? getOrganizationNodeStyle() : getCharacterNodeStyle(node.role_type);
        const detail = detailMap[node.id];
        
        const roleColorMap: Record<string, string> = {
          protagonist: token.colorError,
          antagonist: token.colorPrimary,
          supporting: token.colorInfo,
        };
        const baseColor = roleColorMap[node.role_type] || token.colorInfo;

        const labelContent = node.type === 'organization' ? (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', width: '100%', height: '100%' }}>
            <ApartmentOutlined style={{ fontSize: 24, color: token.colorSuccess, marginBottom: 4 }} />
            <div style={{ fontWeight: 600, fontSize: 14, color: token.colorText, maxWidth: '90%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{node.name}</div>
            <div style={{ fontSize: 11, color: token.colorTextSecondary, marginTop: 2 }}>{detail?.organization_type || '组织'}</div>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', width: '100%', height: '100%' }}>
            {detail?.avatar_url ? (
               <img src={detail.avatar_url} alt={node.name} style={{ width: 56, height: 56, borderRadius: '50%', objectFit: 'cover', border: `2px solid ${token.colorBgContainer}`, boxShadow: `0 2px 6px ${alphaColor(token.colorTextBase, 0.18)}`, marginBottom: 6 }} />
            ) : (
               <div style={{ width: 56, height: 56, borderRadius: '50%', backgroundColor: token.colorFillSecondary, display: 'flex', alignItems: 'center', justifyContent: 'center', border: `2px solid ${token.colorBgContainer}`, boxShadow: `0 2px 6px ${alphaColor(token.colorTextBase, 0.18)}`, marginBottom: 6 }}>
                 <UserOutlined style={{ fontSize: 28, color: baseColor }} />
               </div>
            )}
            <div style={{ fontWeight: 600, fontSize: 13, color: token.colorText, maxWidth: '90%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{node.name}</div>
            <div style={{ fontSize: 11, color: baseColor, marginTop: 2, transform: 'scale(0.9)' }}>
              {node.role_type === 'protagonist' ? '主角' : node.role_type === 'antagonist' ? '反派' : '配角'}
            </div>
          </div>
        );

        return {
          id: node.id,
          type: 'default',
          position: { x: 0, y: 0 },
          data: {
            label: labelContent,
            type: node.type,
            role_type: node.role_type,
          },
          style,
        };
      });

      const mainCareerNodes: Node[] = (careersData.main_careers || []).map((career) => ({
        id: `career-main-${career.id}`,
        type: 'default',
        position: { x: 0, y: 0 },
        data: {
          label: (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
              <div style={{ fontSize: 11, color: token.colorWarning, marginBottom: 2 }}>主职业</div>
              <div style={{ fontWeight: 600, fontSize: 13, color: token.colorText }}>{career.name}</div>
            </div>
          ),
          type: 'career_main',
        },
        style: getCareerNodeStyle('main'),
      }));

      const subCareerNodes: Node[] = (careersData.sub_careers || []).map((career) => ({
        id: `career-sub-${career.id}`,
        type: 'default',
        position: { x: 0, y: 0 },
        data: {
          label: (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
              <div style={{ fontSize: 11, color: token.colorInfo, marginBottom: 2 }}>副职业</div>
              <div style={{ fontWeight: 600, fontSize: 13, color: token.colorText }}>{career.name}</div>
            </div>
          ),
          type: 'career_sub',
        },
        style: getCareerNodeStyle('sub'),
      }));

      const careerGroupNodes: Node[] = [];
      if (mainCareerNodes.length > 0) {
        careerGroupNodes.push({
          id: GROUP_MAIN_CAREER_NODE_ID,
          type: 'default',
          position: { x: 0, y: 0 },
          data: {
            label: '主职业分组',
            type: 'career_group',
          },
          style: getCareerGroupStyle('main'),
        });
      }
      if (subCareerNodes.length > 0) {
        careerGroupNodes.push({
          id: GROUP_SUB_CAREER_NODE_ID,
          type: 'default',
          position: { x: 0, y: 0 },
          data: {
            label: '副职业分组',
            type: 'career_group',
          },
          style: getCareerGroupStyle('sub'),
        });
      }

      const allNodes: Node[] = [...baseNodes, ...careerGroupNodes, ...mainCareerNodes, ...subCareerNodes];

      const orgMemberLinks = data.links.filter((link) => link.relationship.startsWith('组织成员·'));
      const memberRelationLinks = data.links.filter((link) => !link.relationship.startsWith('组织成员·'));

      // 先建立组织-成员边（用于先稳定层级结构）
      const orgMemberEdges: Edge[] = orgMemberLinks.map((link) =>
        buildFlowEdge(
          `${link.source}-${link.target}-${link.relationship}`,
          link.source,
          link.target,
          link.relationship,
          link.status,
          link.intimacy,
          { layoutWeight: 8 },
        ),
      );

      // 再构建职业块 -> 职业 -> 角色边
      const careerGroupEdges: Edge[] = [
        ...mainCareerNodes.map((node) =>
          buildFlowEdge(
            `${GROUP_MAIN_CAREER_NODE_ID}-${node.id}`,
            GROUP_MAIN_CAREER_NODE_ID,
            node.id,
            '职业分类·主职业',
            'active',
            0,
            { dashed: true, layoutWeight: 4 },
          ),
        ),
        ...subCareerNodes.map((node) =>
          buildFlowEdge(
            `${GROUP_SUB_CAREER_NODE_ID}-${node.id}`,
            GROUP_SUB_CAREER_NODE_ID,
            node.id,
            '职业分类·副职业',
            'active',
            0,
            { dashed: true, layoutWeight: 4 },
          ),
        ),
      ];

      const careerToCharacterEdges: Edge[] = [];
      const localCareerNameMap: Record<string, string> = {};
      [...(careersData.main_careers || []), ...(careersData.sub_careers || [])].forEach((career) => {
        localCareerNameMap[career.id] = career.name;
      });

      characters
        .filter((character) => !character.is_organization)
        .forEach((character) => {
          if (character.main_career_id) {
            const careerNodeId = `career-main-${character.main_career_id}`;
            if (mainCareerNodes.some((node) => node.id === careerNodeId)) {
              const careerName = localCareerNameMap[character.main_career_id] || '未知职业';
              careerToCharacterEdges.push(
                buildFlowEdge(
                  `${careerNodeId}-${character.id}-main`,
                  careerNodeId,
                  character.id,
                  `主职业·${careerName}`,
                  'active',
                  100,
                  { layoutWeight: 3 },
                ),
              );
            }
          }

          const subCareerData = safeParseSubCareers(character.sub_careers);
          subCareerData.forEach((sub) => {
            const careerNodeId = `career-sub-${sub.career_id}`;
            if (subCareerNodes.some((node) => node.id === careerNodeId)) {
              const careerName = localCareerNameMap[sub.career_id] || '未知副职业';
              careerToCharacterEdges.push(
                buildFlowEdge(
                  `${careerNodeId}-${character.id}-sub-${sub.stage || 1}`,
                  careerNodeId,
                  character.id,
                  `副职业·${careerName}`,
                  'active',
                  80,
                  { dashed: true, layoutWeight: 2 },
                ),
              );
            }
          });
        });

      // 最后才连接成员之间的人际关系
      const memberRelationEdges: Edge[] = memberRelationLinks.map((link) =>
        buildFlowEdge(
          `${link.source}-${link.target}-${link.relationship}`,
          link.source,
          link.target,
          link.relationship,
          link.status,
          link.intimacy,
          { layoutWeight: 1 },
        ),
      );

      const layoutEdges = [...orgMemberEdges, ...careerGroupEdges, ...careerToCharacterEdges];
      const fallbackLayoutEdges = layoutEdges.length > 0 ? layoutEdges : memberRelationEdges;

      const layouted = getLayoutedElements(allNodes, fallbackLayoutEdges);

      setNodes(layouted.nodes);
      setEdges([...orgMemberEdges, ...careerGroupEdges, ...careerToCharacterEdges, ...memberRelationEdges]);
      setGraphData(data);
    } catch (error) {
      message.error('加载关系图谱失败');
      console.error(error);
    } finally {
      setLoading(false);
    }
  }, [
    projectId,
    relationshipTypes,
    buildFlowEdge,
    setNodes,
    setEdges,
    token.colorBgContainer,
    token.colorError,
    token.colorFillSecondary,
    token.colorInfo,
    token.colorPrimary,
    token.colorSuccess,
    token.colorText,
    token.colorTextBase,
    token.colorTextSecondary,
    token.colorWarning,
  ]);

  // 当 relationshipTypes 加载完成后再加载图数据
  useEffect(() => {
    void loadGraphData();
  }, [loadGraphData]);

  const loadNodeDetail = async (nodeId: string) => {
    if (!projectId) return;

    // 职业分组节点不展示详情
    if (nodeId === GROUP_MAIN_CAREER_NODE_ID || nodeId === GROUP_SUB_CAREER_NODE_ID) {
      return;
    }

    // 职业节点不展示详情
    if (nodeId.startsWith('career-main-') || nodeId.startsWith('career-sub-')) {
      return;
    }

    const cached = characterDetailMap[nodeId];
    if (cached) {
      setNodeDetail(cached);
      return;
    }

    setDetailLoading(true);
    try {
      const res = await axios.get(`/api/characters/${nodeId}`);
      setNodeDetail(res.data as CharacterDetail);
    } catch (error) {
      message.error('加载详情失败');
      console.error(error);
    } finally {
      setDetailLoading(false);
    }
  };

  const handleNodeClick = (_: unknown, node: { id: string }) => {
    setSelectedNodeId(node.id);

    const shouldShowDetail =
      node.id !== GROUP_MAIN_CAREER_NODE_ID &&
      node.id !== GROUP_SUB_CAREER_NODE_ID &&
      !node.id.startsWith('career-main-') &&
      !node.id.startsWith('career-sub-');

    setNodeDetail(null);

    if (shouldShowDetail) {
      void loadNodeDetail(node.id);
    }
  };

  const handleCloseDetail = () => {
    setSelectedNodeId(null);
    setNodeDetail(null);
  };

  const goBack = () => {
    if (projectId) {
      navigate(`/project/${projectId}/relationships`);
      return;
    }
    navigate('/projects');
  };

  const renderCareerTags = () => {
    if (!nodeDetail || nodeDetail.is_organization) return null;

    const subCareerData = safeParseSubCareers(nodeDetail.sub_careers);

    return (
      <div
        style={{
          marginBottom: 12,
          padding: '12px 14px',
          borderRadius: 12,
          background: token.colorFillQuaternary,
          border: `1px solid ${token.colorBorderSecondary}`,
          boxShadow: `0 2px 4px ${alphaColor(token.colorTextBase, 0.06)}`,
        }}
      >
        <Text strong style={{ fontSize: 14, color: token.colorText }}>
          职业体系
        </Text>
        <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {nodeDetail.main_career_id ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Tag color="gold" style={{ margin: 0, borderRadius: 12, padding: '0 10px', fontWeight: 500 }}>主职业</Tag>
              <span style={{ fontSize: 14, color: token.colorText }}>
                {careerNameMap[nodeDetail.main_career_id]?.name || nodeDetail.main_career_id}
                {nodeDetail.main_career_stage ? <span style={{ color: token.colorTextTertiary, marginLeft: 4 }}>第{nodeDetail.main_career_stage}阶</span> : ''}
              </span>
            </div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Tag style={{ margin: 0, borderRadius: 12, padding: '0 10px' }}>主职业</Tag>
              <span style={{ fontSize: 14, color: token.colorTextTertiary }}>未设置</span>
            </div>
          )}

          {subCareerData.length > 0 ? (
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
               <Tag color="cyan" style={{ margin: 0, borderRadius: 12, padding: '0 10px', fontWeight: 500 }}>副职业</Tag>
               <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, flex: 1 }}>
                {subCareerData.map((sub, index) => (
                  <span key={`${sub.career_id}-${index}`} style={{ fontSize: 14, color: token.colorText, background: token.colorBgContainer, border: `1px solid ${token.colorBorderSecondary}`, borderRadius: token.borderRadiusSM, padding: '0 6px' }}>
                    {careerNameMap[sub.career_id]?.name || sub.career_id}
                    {sub.stage ? <span style={{ color: token.colorTextTertiary, marginLeft: 4 }}>阶{sub.stage}</span> : ''}
                  </span>
                ))}
               </div>
            </div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Tag style={{ margin: 0, borderRadius: 12, padding: '0 10px' }}>副职业</Tag>
              <span style={{ fontSize: 14, color: token.colorTextTertiary }}>未设置</span>
            </div>
          )}
        </div>
      </div>
    );
  };

  const traitList = safeParseStringArray(nodeDetail?.traits);
  const orgMembers = safeParseStringArray(nodeDetail?.organization_members);

  return (
    <div
      style={{
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        backgroundColor: token.colorBgLayout,
        overflow: 'hidden',
      }}
    >
      <Card
        size="small"
        style={{
          flex: 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
        }}
        bodyStyle={{
          flex: 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          padding: 12,
        }}
        title={
          <Space>
            <Button type="text" icon={<ArrowLeftOutlined />} onClick={goBack}>
              返回
            </Button>
            <span>关系图谱</span>
            <Tag color="processing" style={{ marginInlineStart: 4 }}>
              {graphData?.nodes?.length || 0} 节点 / {graphData?.links?.length || 0} 关系
            </Tag>
          </Space>
        }
        extra={
          <Space direction="vertical" size={6} style={{ alignItems: 'flex-end' }}>
            <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 14, fontSize: 12, flexWrap: 'wrap' }}>
              {/* 节点图例 */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ color: token.colorInfo, fontWeight: 'bold' }}>●</span>
                <span>角色（圆形）</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ color: token.colorSuccess, fontWeight: 'bold' }}>■</span>
                <span>组织（方形）</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ color: token.colorWarning, fontWeight: 'bold' }}>▭</span>
                <span>主职业</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ color: token.colorInfo, fontWeight: 'bold' }}>▭</span>
                <span>副职业</span>
              </div>

              <span style={{ color: token.colorBorder }}>|</span>

              {/* 连线图例 */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ color: token.colorPrimary, fontWeight: 'bold' }}>- -</span>
                <span>组织成员</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ color: token.colorWarning, fontWeight: 'bold' }}>—</span>
                <span>主职业关联</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ color: token.colorInfo, fontWeight: 'bold' }}>- -</span>
                <span>副职业关联</span>
              </div>
            </div>

            {edgeCategoryOptions.length > 0 && (
              <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  连线显示：
                </Text>
                {edgeCategoryOptions.map((option) => {
                  const isVisible = edgeVisibilityMap[option.category] !== false;
                  return (
                    <Button
                      key={option.category}
                      size="small"
                      type={isVisible ? 'primary' : 'default'}
                      onClick={() => toggleEdgeCategoryVisibility(option.category)}
                      style={
                        isVisible
                          ? { backgroundColor: option.color, borderColor: option.color, color: token.colorWhite }
                          : { color: token.colorTextSecondary }
                      }
                    >
                      {option.label}（{option.count}）
                    </Button>
                  );
                })}
              </div>
            )}
          </Space>
        }
      >
        <div style={{ flex: 1, minHeight: 0 }} className="relationship-graph-flow">
          <style>
            {`
              .relationship-graph-flow .react-flow__handle {
                opacity: 0 !important;
                background: transparent !important;
                border: none !important;
                pointer-events: none !important;
              }

              .relationship-graph-flow .react-flow__node {
                outline: 1px solid var(--ant-color-border-secondary);
                outline-offset: 0;
              }

              .relationship-graph-flow .react-flow__controls {
                border: 1px solid var(--ant-color-border-secondary);
                border-radius: var(--ant-border-radius-lg);
                overflow: hidden;
                background: var(--ant-color-bg-elevated);
                box-shadow: 0 6px 16px color-mix(in srgb, var(--ant-color-text) 12%, transparent);
              }

              .relationship-graph-flow .react-flow__controls-button {
                background: var(--ant-color-bg-elevated);
                border-bottom: 1px solid var(--ant-color-border-secondary);
                color: var(--ant-color-text);
              }

              .relationship-graph-flow .react-flow__controls-button:last-child {
                border-bottom: none;
              }

              .relationship-graph-flow .react-flow__controls-button:hover {
                background: var(--ant-color-fill-secondary);
              }

              .relationship-graph-flow .react-flow__controls-button:disabled {
                background: var(--ant-color-fill-quaternary);
                color: var(--ant-color-text-quaternary);
              }

              .relationship-graph-flow .react-flow__controls-button svg {
                fill: currentColor;
              }

              .relationship-graph-flow .react-flow__attribution {
                background: var(--ant-color-bg-elevated);
                border: 1px solid var(--ant-color-border-secondary);
                border-radius: var(--ant-border-radius-sm);
                box-shadow: 0 2px 8px color-mix(in srgb, var(--ant-color-text) 10%, transparent);
              }

              .relationship-graph-flow .react-flow__attribution a {
                color: var(--ant-color-text-secondary);
              }

              .relationship-graph-flow .react-flow__attribution a:hover {
                color: var(--ant-color-primary);
              }
            `}
          </style>
          <ReactFlow
            nodes={nodes}
            edges={visibleEdges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeClick={handleNodeClick}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            attributionPosition="bottom-left"
          >
            <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
            <Controls position="top-left" />
          </ReactFlow>
        </div>
      </Card>

      {/* 节点详情 */}
{selectedNodeId && nodeDetail && (
<div
  style={{
    position: 'fixed',
    right: 24,
    top: 80,
    width: 400,
    height: 'calc(100vh - 100px)',
    maxHeight: 700,
    zIndex: 1000,
    display: 'flex',
    flexDirection: 'column',
  }}
>
  <Card
    size="small"
    style={{
      width: '100%',
      flex: 1,
      borderRadius: 16,
      boxShadow: `0 12px 32px ${alphaColor(token.colorTextBase, 0.22)}`,
      overflow: 'hidden',
      display: 'flex',
      flexDirection: 'column',
    }}
    bodyStyle={{
      flex: 1,
      overflow: 'hidden',
      padding: '12px 16px',
      display: 'flex',
      flexDirection: 'column',
    }}
            title={
              <Space>
                {nodeDetail.is_organization ? <ApartmentOutlined /> : <UserOutlined />}
                <span>{nodeDetail.is_organization ? '组织详情' : '角色详情'}</span>
              </Space>
            }
            extra={
              <Button type="text" size="small" onClick={handleCloseDetail}>
                ×
              </Button>
            }
          >
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              <div
                style={{
                  textAlign: 'center',
                  marginBottom: 16,
                  padding: '8px 12px 0',
                  minHeight: 140,
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                }}
              >
                <div style={{ position: 'relative', width: 84, height: 84, marginBottom: 12 }}>
                  {nodeDetail.avatar_url ? (
                    <img
                      src={nodeDetail.avatar_url}
                      alt={nodeDetail.name}
                      style={{
                        width: '100%',
                        height: '100%',
                        borderRadius: '50%',
                        objectFit: 'cover',
                        border: `3px solid ${token.colorBgContainer}`,
                        boxShadow: `0 4px 12px ${alphaColor(token.colorTextBase, 0.18)}`,
                      }}
                    />
                  ) : (
                    <div
                      style={{
                        width: '100%',
                        height: '100%',
                        borderRadius: '50%',
                        backgroundColor: nodeDetail.color || (nodeDetail.is_organization ? token.colorSuccess : token.colorPrimary),
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        fontSize: 32,
                        color: token.colorWhite,
                        border: `3px solid ${token.colorBgContainer}`,
                        boxShadow: `0 4px 12px ${alphaColor(token.colorTextBase, 0.18)}`,
                      }}
                    >
                      {nodeDetail.is_organization ? <TeamOutlined /> : <UserOutlined />}
                    </div>
                  )}
                  <div
                    style={{
                      position: 'absolute',
                      bottom: -4,
                      right: -4,
                      background: nodeDetail.is_organization ? token.colorSuccess : (nodeDetail.role_type === 'protagonist' ? token.colorError : nodeDetail.role_type === 'antagonist' ? token.colorPrimary : token.colorInfo),
                      borderRadius: '50%',
                      width: 28,
                      height: 28,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      border: `2px solid ${token.colorBgContainer}`,
                      color: token.colorWhite,
                      boxShadow: `0 2px 6px ${alphaColor(token.colorTextBase, 0.22)}`,
                    }}
                  >
                    {nodeDetail.is_organization ? <ApartmentOutlined style={{ fontSize: 14 }} /> : <UserOutlined style={{ fontSize: 14 }} />}
                  </div>
                </div>

                <div style={{ fontSize: 20, fontWeight: 600, color: token.colorText, marginBottom: 8 }}>{nodeDetail.name}</div>
                <Space size={6} wrap style={{ justifyContent: 'center' }}>
                  {!nodeDetail.is_organization && (
                    <Tag
                      color={
                        nodeDetail.role_type === 'protagonist'
                          ? 'red'
                          : nodeDetail.role_type === 'antagonist'
                            ? 'purple'
                            : 'blue'
                      }
                      style={{ borderRadius: 12, padding: '0 10px', fontWeight: 500 }}
                    >
                      {nodeDetail.role_type === 'protagonist'
                        ? '主角'
                        : nodeDetail.role_type === 'antagonist'
                          ? '反派'
                          : '配角'}
                    </Tag>
                  )}
                  {nodeDetail.gender && !nodeDetail.is_organization && <Tag style={{ borderRadius: 12, padding: '0 10px' }}>{nodeDetail.gender}</Tag>}
                  {nodeDetail.age && !nodeDetail.is_organization && <Tag style={{ borderRadius: 12, padding: '0 10px' }}>{nodeDetail.age}岁</Tag>}
                </Space>
              </div>

              <div style={{ flex: 1, overflowY: 'auto', paddingRight: 8, paddingLeft: 4, paddingBottom: 16 }}>
                {!nodeDetail.is_organization ? (
                  <>
                    {renderCareerTags()}
                    <InfoField label="外貌特征" value={nodeDetail.appearance} rows={2} />
                    <InfoField label="性格特点" value={nodeDetail.personality} rows={3} />
                    <InfoField label="背景故事" value={nodeDetail.background} rows={4} />

                    {traitList.length > 0 && (
                      <div
                        style={{
                          marginBottom: 12,
                          padding: '12px 14px',
                          borderRadius: 12,
                          background: token.colorFillQuaternary,
                          border: `1px solid ${token.colorBorderSecondary}`,
                          boxShadow: `0 2px 4px ${alphaColor(token.colorTextBase, 0.06)}`,
                        }}
                      >
                        <Text strong style={{ fontSize: 14, color: token.colorText }}>
                          特征标签
                        </Text>
                        <Space size={[6, 8]} wrap style={{ marginTop: 10 }}>
                          {traitList.slice(0, 12).map((trait, index) => (
                            <Tag key={`${trait}-${index}`} color="blue" style={{ borderRadius: 12, padding: '0 10px', margin: 0 }}>
                              {trait}
                            </Tag>
                          ))}
                        </Space>
                      </div>
                    )}
                  </>
                ) : (
                  <>
                    <InfoField label="组织类型" value={nodeDetail.organization_type} rows={2} />
                    <InfoField label="组织目的" value={nodeDetail.organization_purpose} rows={3} />
                    <InfoField label="所在地" value={nodeDetail.location} rows={2} />
                    <InfoField label="组织格言" value={nodeDetail.motto} rows={2} />

                    {nodeDetail.power_level !== undefined && nodeDetail.power_level !== null && (
                      <div
                        style={{
                          marginBottom: 12,
                          padding: '12px 14px',
                          borderRadius: 12,
                          background: token.colorFillQuaternary,
                          border: `1px solid ${token.colorBorderSecondary}`,
                          boxShadow: `0 2px 4px ${alphaColor(token.colorTextBase, 0.06)}`,
                        }}
                      >
                        <Text strong style={{ fontSize: 14, color: token.colorText }}>
                          势力等级
                        </Text>
                        <div style={{ ...clampTextStyle(1), fontSize: 18, color: token.colorWarning, fontWeight: 'bold' }}>
                          {nodeDetail.power_level}<span style={{ fontSize: 14, color: token.colorTextTertiary, fontWeight: 'normal' }}>/100</span>
                        </div>
                      </div>
                    )}

                    {orgMembers.length > 0 && (
                      <div
                        style={{
                          marginBottom: 12,
                          padding: '12px 14px',
                          borderRadius: 12,
                          background: token.colorFillQuaternary,
                          border: `1px solid ${token.colorBorderSecondary}`,
                          boxShadow: `0 2px 4px ${alphaColor(token.colorTextBase, 0.06)}`,
                        }}
                      >
                        <Text strong style={{ fontSize: 14, color: token.colorText }}>
                          组织成员
                        </Text>
                        <Space size={[6, 8]} wrap style={{ marginTop: 10 }}>
                          {orgMembers.slice(0, 16).map((member, index) => (
                            <Tag key={`${member}-${index}`} color="green" style={{ borderRadius: 12, padding: '0 10px', margin: 0 }}>
                              {member}
                            </Tag>
                          ))}
                        </Space>
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          </Card>
        </div>
      )}

      {/* 职业节点点击提示（不展示详情卡时） */}
      {selectedNodeId && !nodeDetail && (selectedNodeId.startsWith('career-main-') || selectedNodeId.startsWith('career-sub-')) && (
        <div
          style={{
            position: 'fixed',
            right: 20,
            top: 80,
            zIndex: 1000,
          }}
        >
          <Card size="small" style={{ width: 300, borderRadius: 10, boxShadow: `0 6px 18px ${alphaColor(token.colorTextBase, 0.2)}` }}>
            <Space align="start">
              <TrophyOutlined style={{ color: token.colorWarning, marginTop: 4 }} />
              <div>
                <Text strong>职业节点</Text>
                <p style={{ ...clampTextStyle(2), marginTop: 2 }}>
                  职业节点用于展示主/副职业分组及其与角色的关联关系，不显示角色详情卡。
                </p>
              </div>
            </Space>
          </Card>
        </div>
      )}
    </div>
  );
}
