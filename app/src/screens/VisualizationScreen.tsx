import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from "react";
import { engineCall } from "../lib/ipc";
import type {
  KnowledgeLeadIdea,
  KnowledgeMap,
  KnowledgeMapEdge,
  KnowledgeMapNode,
  Project,
} from "../types";

interface Props {
  project: Project | null;
}

interface LayoutNode extends KnowledgeMapNode {
  x: number;
  y: number;
  z: number;
  color: string;
}

interface ProjectedNode extends LayoutNode {
  screenX: number;
  screenY: number;
  screenR: number;
  depth: number;
}

interface LabelBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

interface GroupOffset {
  x: number;
  y: number;
  z: number;
}

const GROUP_COLORS = [
  "#2563eb",
  "#0f766e",
  "#c2410c",
  "#7c3aed",
  "#b45309",
  "#1d4ed8",
  "#047857",
  "#be123c",
];

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function hashUnit(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967295;
}

function spherePoint(index: number, total: number, radius: number): { x: number; y: number; z: number } {
  const safeTotal = Math.max(total, 1);
  const golden = Math.PI * (3 - Math.sqrt(5));
  const y = 1 - ((index + 0.5) / safeTotal) * 2;
  const horizontalRadius = Math.sqrt(Math.max(0, 1 - (y * y)));
  const theta = golden * index;
  return {
    x: Math.cos(theta) * horizontalRadius * radius,
    y: y * radius,
    z: Math.sin(theta) * horizontalRadius * radius,
  };
}

function rotatePoint(
  point: { x: number; y: number; z: number },
  angleX: number,
  angleY: number
): { x: number; y: number; z: number } {
  const cosY = Math.cos(angleY);
  const sinY = Math.sin(angleY);
  const x1 = point.x * cosY - point.z * sinY;
  const z1 = point.x * sinY + point.z * cosY;

  const cosX = Math.cos(angleX);
  const sinX = Math.sin(angleX);
  const y2 = point.y * cosX - z1 * sinX;
  const z2 = point.y * sinX + z1 * cosX;

  return { x: x1, y: y2, z: z2 };
}

function createLayout(data: KnowledgeMap): LayoutNode[] {
  if (data.nodes.length === 0) return [];

  const groupIndexById = new Map<string, number>(
    data.groups.map((group, index) => [group.id, index])
  );
  const groupById = new Map(data.groups.map((group) => [group.id, group]));
  const groupedNodes = new Map<string, KnowledgeMapNode[]>();

  for (const node of data.nodes) {
    const existing = groupedNodes.get(node.group) ?? [];
    existing.push(node);
    groupedNodes.set(node.group, existing);
  }

  const groupIds = Array.from(groupedNodes.keys()).sort((left, right) => {
    const leftIndex = groupIndexById.get(left) ?? 0;
    const rightIndex = groupIndexById.get(right) ?? 0;
    return leftIndex - rightIndex;
  });

  const groupCenters = new Map<string, { x: number; y: number; z: number }>();
  const averageGroupSize =
    groupIds.reduce((sum, groupId) => sum + (groupById.get(groupId)?.size ?? 1), 0) / Math.max(groupIds.length, 1);
  const groupOrbit = groupIds.length <= 1 ? 0 : clamp(1.55 + groupIds.length * 0.26 + averageGroupSize * 0.04, 1.6, 3.35);
  const verticalScale = groupIds.length > 4 ? 0.62 : 0.78;
  groupIds.forEach((groupId, index) => {
    if (groupIds.length === 1) {
      groupCenters.set(groupId, { x: 0, y: 0, z: 0 });
      return;
    }
    const angle = ((Math.PI * 2) * index) / groupIds.length - (Math.PI / 2);
    groupCenters.set(groupId, {
      x: Math.cos(angle) * groupOrbit,
      y: Math.sin(angle) * groupOrbit * verticalScale,
      z: Math.sin(angle * 1.75) * 0.95,
    });
  });

  const layout: LayoutNode[] = [];
  for (const groupId of groupIds) {
    const center = groupCenters.get(groupId)!;
    const groupMeta = groupById.get(groupId);
    const nodes = (groupedNodes.get(groupId) ?? []).slice().sort((left, right) =>
      left.label.localeCompare(right.label)
    );
    const clusterRadius = clamp(
      0.28 + Math.sqrt(nodes.length) * 0.1 + ((groupMeta?.explicit_edge_count ?? 0) * 0.008),
      0.3,
      0.92
    );
    const color = GROUP_COLORS[(groupIndexById.get(groupId) ?? 0) % GROUP_COLORS.length];

    nodes.forEach((node, index) => {
      const local = spherePoint(index, nodes.length, clusterRadius);
      const noiseX = (hashUnit(`${node.id}:x`) - 0.5) * 0.12;
      const noiseY = (hashUnit(`${node.id}:y`) - 0.5) * 0.12;
      const noiseZ = (hashUnit(`${node.id}:z`) - 0.5) * 0.12;
      layout.push({
        ...node,
        color,
        x: center.x + local.x * 1.18 + noiseX,
        y: center.y + local.y * 0.92 + noiseY,
        z: center.z + local.z * 1.12 + noiseZ,
      });
    });
  }

  const centersByGroup = new Map(groupCenters);
  for (let iteration = 0; iteration < 16; iteration += 1) {
    for (let i = 0; i < layout.length; i += 1) {
      const node = layout[i];
      const center = centersByGroup.get(node.group);
      if (center) {
        node.x += (center.x - node.x) * 0.012;
        node.y += (center.y - node.y) * 0.012;
        node.z += (center.z - node.z) * 0.012;
      }
      for (let j = i + 1; j < layout.length; j += 1) {
        const other = layout[j];
        const dx = node.x - other.x;
        const dy = node.y - other.y;
        const dz = node.z - other.z;
        const distance = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.0001;
        const desired = node.group === other.group ? 0.34 : 0.54;
        if (distance >= desired) continue;
        const push = ((desired - distance) / desired) * 0.028;
        const nx = dx / distance;
        const ny = dy / distance;
        const nz = dz / distance;
        node.x += nx * push;
        node.y += ny * push;
        node.z += nz * push;
        other.x -= nx * push;
        other.y -= ny * push;
        other.z -= nz * push;
      }
    }
  }

  return layout;
}

function buildLabelCandidates(
  projected: ProjectedNode[],
  selectedNodeId: string | null,
  hoveredNodeId: string | null,
  nodeById: Map<string, LayoutNode>
): ProjectedNode[] {
  const projectedById = new Map(projected.map((node) => [node.id, node]));
  const orderedIds: string[] = [];
  const seen = new Set<string>();
  const push = (nodeId: string | null | undefined) => {
    if (!nodeId || seen.has(nodeId)) return;
    if (!projectedById.has(nodeId)) return;
    seen.add(nodeId);
    orderedIds.push(nodeId);
  };

  push(selectedNodeId);
  push(hoveredNodeId);

  for (const idea of nodeById.get(selectedNodeId ?? "")?.lead_ideas ?? []) {
    push(idea.id);
  }
  for (const idea of nodeById.get(hoveredNodeId ?? "")?.lead_ideas ?? []) {
    push(idea.id);
  }

  const topPerGroup = new Map<string, ProjectedNode>();
  for (const node of projected) {
    const existing = topPerGroup.get(node.group);
    if (!existing || node.degree > existing.degree || node.source_count > existing.source_count) {
      topPerGroup.set(node.group, node);
    }
  }
  for (const node of topPerGroup.values()) {
    push(node.id);
  }

  projected
    .slice()
    .sort((left, right) =>
      right.degree - left.degree ||
      right.source_count - left.source_count ||
      left.label.localeCompare(right.label)
    )
    .slice(0, 10)
    .forEach((node) => push(node.id));

  return orderedIds
    .map((nodeId) => projectedById.get(nodeId))
    .filter((node): node is ProjectedNode => Boolean(node));
}

function boxesOverlap(a: LabelBox, b: LabelBox): boolean {
  return !(a.right < b.left || a.left > b.right || a.bottom < b.top || a.top > b.bottom);
}

function roundedRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number
): void {
  const r = Math.min(radius, width / 2, height / 2);
  context.beginPath();
  context.moveTo(x + r, y);
  context.lineTo(x + width - r, y);
  context.quadraticCurveTo(x + width, y, x + width, y + r);
  context.lineTo(x + width, y + height - r);
  context.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  context.lineTo(x + r, y + height);
  context.quadraticCurveTo(x, y + height, x, y + height - r);
  context.lineTo(x, y + r);
  context.quadraticCurveTo(x, y, x + r, y);
  context.closePath();
}

function relatedLabel(idea: KnowledgeLeadIdea): string {
  if (idea.relation_type === "shared_source") {
    return idea.shared_sources > 0 ? `${idea.shared_sources} shared source${idea.shared_sources === 1 ? "" : "s"}` : "shared source";
  }
  return idea.relation_type.replace(/_/g, " ");
}

function provenanceBadgeLabel(idea: KnowledgeLeadIdea): string {
  if (idea.provenance_kind === "hybrid") {
    return "hybrid";
  }
  if (idea.provenance_kind === "explicit_relation") {
    return "explicit";
  }
  return "inferred";
}

export function VisualizationScreen({ project }: Props) {
  const [map, setMap] = useState<KnowledgeMap | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const projectedNodesRef = useRef<ProjectedNode[]>([]);
  const groupOffsetsRef = useRef<Record<string, GroupOffset>>({});
  const viewRef = useRef({ rotationX: 0, rotationY: 0, zoom: 1 });
  const interactionRef = useRef<{
    mode: "none" | "rotate" | "group";
    startX: number;
    startY: number;
    groupId: string | null;
    moved: boolean;
  }>({
    mode: "none",
    startX: 0,
    startY: 0,
    groupId: null,
    moved: false,
  });
  const suppressClickRef = useRef(false);
  const pauseAutoUntilRef = useRef(0);

  const load = async () => {
    if (!project) return;
    setLoading(true);
    setError(null);
    try {
      const result = await engineCall<KnowledgeMap>("graph.knowledgeMap", {
        project_id: project.project_id,
      });
      setMap(result);
      setSelectedNodeId((current) => {
        if (current && result.nodes.some((node) => node.id === current)) {
          return current;
        }
        const topNode = result.nodes
          .slice()
          .sort((left, right) => right.degree - left.degree || right.source_count - left.source_count)[0];
        return topNode?.id ?? null;
      });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [project?.project_id]);

  useEffect(() => {
    if (!map) return;
    const nextOffsets: Record<string, GroupOffset> = {};
    for (const group of map.groups) {
      nextOffsets[group.id] = { x: 0, y: 0, z: 0 };
    }
    groupOffsetsRef.current = nextOffsets;
    viewRef.current = { rotationX: 0, rotationY: 0, zoom: 1 };
    interactionRef.current = { mode: "none", startX: 0, startY: 0, groupId: null, moved: false };
  }, [map]);

  const layoutNodes = useMemo(
    () =>
      createLayout(
        map ?? {
          project_id: project?.project_id ?? "",
          generated_at: "",
          nodes: [],
          edges: [],
          groups: [],
          stats: {
            node_count: 0,
            edge_count: 0,
            group_count: 0,
            explicit_edge_count: 0,
            inferred_edge_count: 0,
          },
        }
      ),
    [map, project?.project_id]
  );
  const nodeById = useMemo(
    () => new Map(layoutNodes.map((node) => [node.id, node])),
    [layoutNodes]
  );
  const selectedNode = selectedNodeId ? nodeById.get(selectedNodeId) ?? null : null;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !map) return;

    const context = canvas.getContext("2d");
    if (!context) return;

    let animationFrame = 0;
    let mounted = true;
    let width = 0;
    let height = 0;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      width = rect.width;
      height = rect.height;
      canvas.width = Math.max(1, Math.round(rect.width * dpr));
      canvas.height = Math.max(1, Math.round(rect.height * dpr));
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    resize();
    const observer = new ResizeObserver(() => resize());
    observer.observe(canvas);

    const draw = (timestamp: number) => {
      if (!mounted) return;

      context.clearRect(0, 0, width, height);

      const gradient = context.createLinearGradient(0, 0, width, height);
      gradient.addColorStop(0, "#f8fbff");
      gradient.addColorStop(1, "#eef3f8");
      context.fillStyle = gradient;
      context.fillRect(0, 0, width, height);

      const autoRotation = performance.now() > pauseAutoUntilRef.current ? timestamp * 0.00016 : 0;
      const angleY = autoRotation + viewRef.current.rotationY;
      const angleX = clamp(Math.sin(timestamp * 0.00023) * 0.16 + viewRef.current.rotationX, -0.85, 0.85);
      const screenScale =
        Math.min(width, height) *
        clamp(
          0.3 + (Math.min(map.stats.node_count, 90) * 0.0016) + (map.stats.group_count * 0.016),
          0.34,
          0.42
        );
      const projected = layoutNodes.map((node) => {
        const offset = groupOffsetsRef.current[node.group] ?? { x: 0, y: 0, z: 0 };
        const rotated = rotatePoint(
          {
            x: node.x + offset.x,
            y: node.y + offset.y,
            z: node.z + offset.z,
          },
          angleX,
          angleY
        );
        const depth = 4.8 - rotated.z;
        const perspective = 2.1 / Math.max(depth, 1.8);
        const scaled = screenScale * viewRef.current.zoom;
        const screenX = width / 2 + rotated.x * scaled * perspective;
        const screenY = height / 2 + rotated.y * scaled * perspective;
        const screenR = clamp((6 + node.degree * 0.8 + node.source_count * 0.5) * perspective, 3.5, 18);
        return {
          ...node,
          screenX,
          screenY,
          screenR,
          depth: rotated.z,
        };
      });
      projectedNodesRef.current = projected;

      const projectedById = new Map(projected.map((node) => [node.id, node]));
      const selectedSet = new Set<string>();
      if (selectedNodeId) {
        selectedSet.add(selectedNodeId);
        for (const idea of nodeById.get(selectedNodeId)?.lead_ideas ?? []) {
          selectedSet.add(idea.id);
        }
      }

      const sortedEdges = map.edges
        .map((edge) => ({
          edge,
          source: projectedById.get(edge.source),
          target: projectedById.get(edge.target),
        }))
        .filter(
          (item): item is { edge: KnowledgeMapEdge; source: ProjectedNode; target: ProjectedNode } =>
            Boolean(item.source && item.target)
        )
        .sort((left, right) => (left.source.depth + left.target.depth) - (right.source.depth + right.target.depth));

      for (const { edge, source, target } of sortedEdges) {
        const highlighted =
          source.id === selectedNodeId ||
          target.id === selectedNodeId ||
          source.id === hoveredNodeId ||
          target.id === hoveredNodeId;
        context.beginPath();
        context.moveTo(source.screenX, source.screenY);
        context.lineTo(target.screenX, target.screenY);
        context.lineWidth = highlighted ? 2.4 : edge.kind === "explicit" ? 1.6 : 1.1;
        context.strokeStyle =
          edge.kind === "explicit"
            ? `rgba(37, 99, 235, ${highlighted ? 0.55 : 0.22})`
            : `rgba(90, 90, 90, ${highlighted ? 0.35 : 0.12})`;
        context.stroke();
      }

      projected
        .slice()
        .sort((left, right) => left.depth - right.depth)
        .forEach((node) => {
          const isSelected = node.id === selectedNodeId;
          const isHovered = node.id === hoveredNodeId;
          const emphasized = isSelected || isHovered || selectedSet.has(node.id);

          if (emphasized) {
            context.beginPath();
            context.arc(node.screenX, node.screenY, node.screenR + 7, 0, Math.PI * 2);
            context.fillStyle = isSelected
              ? "rgba(37, 99, 235, 0.12)"
              : "rgba(15, 23, 42, 0.07)";
            context.fill();
          }

          context.beginPath();
          context.arc(node.screenX, node.screenY, node.screenR, 0, Math.PI * 2);
          context.fillStyle = node.color;
          context.shadowColor = emphasized ? "rgba(37, 99, 235, 0.22)" : "transparent";
          context.shadowBlur = emphasized ? 14 : 0;
          context.fill();
          context.shadowBlur = 0;

          context.lineWidth = isSelected ? 2.4 : 1;
          context.strokeStyle = isSelected ? "#0f172a" : "rgba(255,255,255,0.9)";
          context.stroke();
        });

      const labelBoxes: LabelBox[] = [];
      const labelCandidates = buildLabelCandidates(projected, selectedNodeId, hoveredNodeId, nodeById);
      for (const node of labelCandidates) {
        const isPrimary = node.id === selectedNodeId || node.id === hoveredNodeId;
        context.font = isPrimary
          ? "600 12px -apple-system, BlinkMacSystemFont, sans-serif"
          : "500 11px -apple-system, BlinkMacSystemFont, sans-serif";
        const textWidth = context.measureText(node.label).width;
        const boxWidth = textWidth + 12;
        const boxHeight = isPrimary ? 22 : 20;
        const boxX = node.screenX + node.screenR + 8;
        const boxY = node.screenY - boxHeight / 2;
        const candidate: LabelBox = {
          left: boxX,
          top: boxY,
          right: boxX + boxWidth,
          bottom: boxY + boxHeight,
        };
        if (
          candidate.right > width - 12 ||
          candidate.left < 12 ||
          candidate.top < 12 ||
          candidate.bottom > height - 12
        ) {
          continue;
        }
        if (labelBoxes.some((box) => boxesOverlap(box, candidate))) {
          continue;
        }

        roundedRect(context, boxX, boxY, boxWidth, boxHeight, 10);
        context.fillStyle = isPrimary ? "rgba(255,255,255,0.92)" : "rgba(255,255,255,0.82)";
        context.fill();
        context.lineWidth = isPrimary ? 1.3 : 1;
        context.strokeStyle = isPrimary ? "rgba(37, 99, 235, 0.45)" : "rgba(15, 23, 42, 0.08)";
        context.stroke();

        context.fillStyle = "#0f172a";
        context.fillText(node.label, boxX + 6, boxY + boxHeight / 2 + 4);
        labelBoxes.push(candidate);
      }

      for (let index = 0; index < 28; index += 1) {
        const x = ((index * 97) % 1000) / 1000;
        const y = ((index * 57 + 123) % 1000) / 1000;
        const radius = 0.8 + ((index * 13) % 5) * 0.22;
        context.beginPath();
        context.arc(x * width, y * height, radius, 0, Math.PI * 2);
        context.fillStyle = "rgba(255,255,255,0.35)";
        context.fill();
      }

      animationFrame = window.requestAnimationFrame(draw);
    };

    animationFrame = window.requestAnimationFrame(draw);
    return () => {
      mounted = false;
      observer.disconnect();
      window.cancelAnimationFrame(animationFrame);
    };
  }, [hoveredNodeId, layoutNodes, map, nodeById, selectedNodeId]);

  const findNodeFromPoint = (clientX: number, clientY: number): ProjectedNode | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    return (
      projectedNodesRef.current
      .slice()
      .sort((left, right) => right.depth - left.depth)
      .find((node) => {
        const dx = node.screenX - x;
        const dy = node.screenY - y;
        return Math.sqrt((dx * dx) + (dy * dy)) <= node.screenR + 5;
      }) ?? null
    );
  };

  const selectNodeFromPoint = (clientX: number, clientY: number, action: "hover" | "select") => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const hit = findNodeFromPoint(clientX, clientY);
    if (action === "hover") {
      setHoveredNodeId(hit?.id ?? null);
      canvas.style.cursor = hit ? "pointer" : "grab";
    } else if (hit) {
      setSelectedNodeId(hit.id);
    }
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const hit = findNodeFromPoint(event.clientX, event.clientY);
    pauseAutoUntilRef.current = performance.now() + 12000;
    suppressClickRef.current = false;
    canvas.setPointerCapture(event.pointerId);
    if (hit) {
      interactionRef.current = {
        mode: "group",
        startX: event.clientX,
        startY: event.clientY,
        groupId: hit.group,
        moved: false,
      };
      setSelectedNodeId(hit.id);
    } else {
      interactionRef.current = {
        mode: "rotate",
        startX: event.clientX,
        startY: event.clientY,
        groupId: null,
        moved: false,
      };
    }
    canvas.style.cursor = "grabbing";
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const interaction = interactionRef.current;
    if (interaction.mode === "rotate") {
      const dx = event.clientX - interaction.startX;
      const dy = event.clientY - interaction.startY;
      if (Math.abs(dx) > 1 || Math.abs(dy) > 1) {
        interaction.moved = true;
        suppressClickRef.current = true;
      }
      interaction.startX = event.clientX;
      interaction.startY = event.clientY;
      viewRef.current.rotationY += dx * 0.0055;
      viewRef.current.rotationX = clamp(viewRef.current.rotationX + dy * 0.0035, -0.85, 0.85);
      setHoveredNodeId(null);
      canvas.style.cursor = "grabbing";
      return;
    }

    if (interaction.mode === "group" && interaction.groupId) {
      const dx = event.clientX - interaction.startX;
      const dy = event.clientY - interaction.startY;
      if (Math.abs(dx) > 1 || Math.abs(dy) > 1) {
        interaction.moved = true;
        suppressClickRef.current = true;
      }
      interaction.startX = event.clientX;
      interaction.startY = event.clientY;
      const zoom = viewRef.current.zoom;
      const offset = groupOffsetsRef.current[interaction.groupId] ?? { x: 0, y: 0, z: 0 };
      offset.x = clamp(offset.x + dx / (240 * zoom), -3.4, 3.4);
      offset.y = clamp(offset.y + dy / (240 * zoom), -2.8, 2.8);
      groupOffsetsRef.current[interaction.groupId] = offset;
      setHoveredNodeId(null);
      canvas.style.cursor = "grabbing";
      return;
    }

    selectNodeFromPoint(event.clientX, event.clientY, "hover");
  };

  const releaseInteraction = (pointerId?: number) => {
    const canvas = canvasRef.current;
    interactionRef.current = { mode: "none", startX: 0, startY: 0, groupId: null, moved: false };
    if (canvas) {
      canvas.style.cursor = hoveredNodeId ? "pointer" : "grab";
      if (pointerId !== undefined) {
        try {
          canvas.releasePointerCapture(pointerId);
        } catch {
          // no-op
        }
      }
    }
  };

  const handleWheel = (event: ReactWheelEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    pauseAutoUntilRef.current = performance.now() + 12000;
    const factor = event.deltaY > 0 ? 0.92 : 1.08;
    viewRef.current.zoom = clamp(viewRef.current.zoom * factor, 0.68, 1.9);
  };

  const resetView = () => {
    if (!map) return;
    viewRef.current = { rotationX: 0, rotationY: 0, zoom: 1 };
    const nextOffsets: Record<string, GroupOffset> = {};
    for (const group of map.groups) {
      nextOffsets[group.id] = { x: 0, y: 0, z: 0 };
    }
    groupOffsetsRef.current = nextOffsets;
    pauseAutoUntilRef.current = 0;
  };

  if (!project) {
    return (
      <div className="screen">
        <div className="screen-header"><h1>Map</h1></div>
        <div className="empty-state">
          <h3>No project open</h3>
          <p>Open a project to inspect its concept graph.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="screen">
      <div className="screen-header">
        <div>
          <h1>3D Knowledge Map</h1>
          <p style={{ fontSize: 12, color: "var(--color-text-muted)", marginTop: 4 }}>
            Drag the background to rotate. Drag a node to move its group. Scroll to zoom.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {map && (
            <span style={{ fontSize: 12, color: "var(--color-text-muted)" }}>
              {map.stats.node_count} nodes · {map.stats.edge_count} links · {map.stats.group_count} groups · {map.stats.explicit_edge_count} explicit · {map.stats.inferred_edge_count} inferred
            </span>
          )}
          <button className="btn" onClick={resetView} disabled={loading || !map}>
            Reset View
          </button>
          <button className="btn" onClick={() => void load()} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      <div className="screen-body knowledge-map-screen">
        {error && (
          <div className="card" style={{ marginBottom: 12, color: "var(--color-danger)" }}>
            {error}
          </div>
        )}

        {loading && !map ? (
          <div className="empty-state">
            <h3>Building the map…</h3>
            <p>Loading concept nodes, links, and groups from the current project.</p>
          </div>
        ) : !loading && map && map.nodes.length === 0 ? (
          <div className="empty-state">
            <h3>No knowledge graph yet</h3>
            <p>
              Run a build after ingesting sources. Once entities and links exist, this page will render the concept network in 3D.
            </p>
          </div>
        ) : (
          <div className="knowledge-map-layout">
            <div className="knowledge-map-canvas-shell card">
              <canvas
                ref={canvasRef}
                className="knowledge-map-canvas"
                onPointerDown={handlePointerDown}
                onPointerMove={handlePointerMove}
                onPointerUp={(event) => releaseInteraction(event.pointerId)}
                onPointerCancel={(event) => releaseInteraction(event.pointerId)}
                onPointerLeave={() => {
                  if (interactionRef.current.mode !== "none") {
                    releaseInteraction();
                  }
                  setHoveredNodeId(null);
                  if (canvasRef.current) {
                    canvasRef.current.style.cursor = "grab";
                  }
                }}
                onClick={(event) => {
                  if (interactionRef.current.mode === "none" && !suppressClickRef.current) {
                    selectNodeFromPoint(event.clientX, event.clientY, "select");
                  }
                  suppressClickRef.current = false;
                }}
                onWheel={handleWheel}
              />
              <div className="knowledge-map-legend">
                {(map?.groups ?? []).slice(0, 6).map((group, index) => (
                  <span
                    key={group.id}
                    className="knowledge-map-legend-item"
                    title={group.rationale}
                  >
                    <span
                      className="knowledge-map-legend-dot"
                      style={{ background: GROUP_COLORS[index % GROUP_COLORS.length] }}
                    />
                    {group.label} ({group.size})
                  </span>
                ))}
              </div>
            </div>

            <aside className="knowledge-map-inspector card">
              {selectedNode ? (
                <>
                  <div className="knowledge-map-node-header">
                    <div>
                      <div className="knowledge-map-node-title">{selectedNode.label}</div>
                      <div className="knowledge-map-node-subtitle">
                        {selectedNode.entity_type} · cluster: {selectedNode.group_label}
                      </div>
                    </div>
                    <span
                      className="knowledge-map-node-swatch"
                      style={{ background: selectedNode.color }}
                    />
                  </div>

                  <p className="knowledge-map-summary">{selectedNode.summary}</p>

                  <div className="knowledge-map-stat-grid">
                    <div>
                      <span className="knowledge-map-stat-label">Links</span>
                      <strong>{selectedNode.degree}</strong>
                    </div>
                    <div>
                      <span className="knowledge-map-stat-label">Sources</span>
                      <strong>{selectedNode.source_count}</strong>
                    </div>
                    <div>
                      <span className="knowledge-map-stat-label">Confidence</span>
                      <strong>{selectedNode.confidence.toFixed(2)}</strong>
                    </div>
                  </div>

                  {selectedNode.page_title && (
                    <div className="knowledge-map-meta-block">
                      <div className="knowledge-map-section-title">Represents</div>
                      <p>{selectedNode.page_title}</p>
                    </div>
                  )}

                  <div className="knowledge-map-meta-block">
                    <div className="knowledge-map-section-title">Cluster Rationale</div>
                    <p>{selectedNode.group_rationale || `Grouped under ${selectedNode.group_label}.`}</p>
                  </div>

                  <div className="knowledge-map-meta-block">
                    <div className="knowledge-map-section-title">Supporting Sources</div>
                    {selectedNode.supporting_sources.length === 0 ? (
                      <p className="knowledge-map-muted">No supporting source titles were linked yet.</p>
                    ) : (
                      <div className="knowledge-map-chip-list">
                        {selectedNode.supporting_sources.map((sourceTitle) => (
                          <span key={sourceTitle} className="knowledge-map-chip">
                            {sourceTitle}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="knowledge-map-meta-block">
                    <div className="knowledge-map-section-title">Leads To / Linked Ideas</div>
                    {selectedNode.lead_ideas.length === 0 ? (
                      <p className="knowledge-map-muted">No linked ideas were extracted yet.</p>
                    ) : (
                      <div className="knowledge-map-idea-list">
                        {selectedNode.lead_ideas.map((idea) => (
                          <button
                            key={idea.id}
                            className={`knowledge-map-idea-button${selectedNodeId === idea.id ? " active" : ""}`}
                            onClick={() => setSelectedNodeId(idea.id)}
                          >
                            <div className="knowledge-map-idea-topline">
                              <span>{idea.label}</span>
                              <span
                                className={`badge ${
                                  idea.provenance_kind === "explicit_relation"
                                    ? ""
                                    : idea.provenance_kind === "hybrid"
                                    ? "badge-yellow"
                                    : "badge-gray"
                                }`}
                              >
                                {provenanceBadgeLabel(idea)}
                              </span>
                            </div>
                            <div className="knowledge-map-idea-meta">
                              {relatedLabel(idea)} · weight {idea.weight.toFixed(2)}
                            </div>
                            <div className="knowledge-map-idea-meta">
                              {idea.provenance_summary}
                            </div>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <div className="empty-state" style={{ height: "100%" }}>
                  <h3>No node selected</h3>
                  <p>Click a node in the rotating graph to inspect what it represents and where it leads.</p>
                </div>
              )}
            </aside>
          </div>
        )}
      </div>
    </div>
  );
}
