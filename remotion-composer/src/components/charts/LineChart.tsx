import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

interface DataPoint {
  x: number;
  y: number;
}

interface Series {
  label: string;
  data: DataPoint[];
  color?: string;
}

type LineAnimationStyle = "draw" | "fade-in";

interface LineChartProps {
  series: Series[];
  title?: string;
  colors?: string[];
  fontFamily?: string;
  textColor?: string;
  backgroundColor?: string;
  gridColor?: string;
  showGrid?: boolean;
  showMarkers?: boolean;
  showLegend?: boolean;
  xLabel?: string;
  yLabel?: string;
  animationStyle?: LineAnimationStyle;
  strokeWidth?: number;
}

export const LineChart: React.FC<LineChartProps> = ({
  series,
  title,
  colors = ["#2563EB", "#F59E0B", "#10B981", "#EC4899", "#06B6D4", "#8B5CF6"],
  fontFamily = "Inter, system-ui, sans-serif",
  textColor = "#1F2937",
  backgroundColor = "#FFFFFF",
  gridColor = "#E5E7EB",
  showGrid = true,
  showMarkers = true,
  showLegend = true,
  xLabel,
  yLabel,
  animationStyle = "draw",
  strokeWidth = 3,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames, width, height } = useVideoConfig();

  // Legend only renders for multi-series charts; don't reserve space otherwise.
  const legendVisible = showLegend && series.length > 1;

  // Chart layout — derived from the actual canvas so the chart fills any
  // orientation (16:9, 9:16, square) instead of a hardcoded 1920x1080 viewBox
  // that SVG aspect-preservation would shrink and letterbox on a portrait frame.
  const centerX = width / 2;
  const marginX = Math.round(width * 0.1);
  const chartLeft = marginX + Math.round(width * 0.037); // extra room for y-axis ticks
  const chartRight = width - marginX;
  const titleY = Math.round(height * 0.06);
  const chartTop = Math.round(height * (title ? 0.16 : 0.1));
  const chartBottom = Math.round(height * (legendVisible ? 0.82 : 0.88));
  const legendY = Math.round(height * 0.9);
  const chartWidth = chartRight - chartLeft;
  const chartHeight = chartBottom - chartTop;

  // Compute data bounds across all series
  const allPoints = series.flatMap((s) => s.data);
  const xMin = Math.min(...allPoints.map((p) => p.x));
  const xMax = Math.max(...allPoints.map((p) => p.x));
  const yMin = 0;
  const yMax = Math.max(...allPoints.map((p) => p.y)) * 1.1; // 10% headroom

  const toSvgX = (x: number) =>
    chartLeft + ((x - xMin) / (xMax - xMin || 1)) * chartWidth;
  const toSvgY = (y: number) =>
    chartBottom - ((y - yMin) / (yMax - yMin || 1)) * chartHeight;

  // Grid
  const gridLineCountY = 5;
  const gridLinesY = Array.from({ length: gridLineCountY + 1 }, (_, i) => {
    const value = (yMax / gridLineCountY) * i;
    const y = toSvgY(value);
    return { value, y };
  });

  const gridLineCountX = Math.min(allPoints.length - 1, 6);
  const gridLinesX = Array.from({ length: gridLineCountX + 1 }, (_, i) => {
    const value = xMin + ((xMax - xMin) / gridLineCountX) * i;
    const x = toSvgX(value);
    return { value, x };
  });

  // Fade out near end
  const fadeOut = interpolate(
    frame,
    [durationInFrames - 15, durationInFrames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  return (
    <AbsoluteFill
      style={{
        background: backgroundColor,
        justifyContent: "flex-start",
        alignItems: "center",
        padding: 40,
      }}
    >
      <svg
        viewBox={`0 0 ${width} ${height}`}
        style={{ width: "100%", height: "100%" }}
      >
        {/* Title */}
        {title && (
          <text
            x={centerX}
            y={titleY}
            textAnchor="middle"
            fill={textColor}
            fontFamily={fontFamily}
            fontWeight={700}
            fontSize={48}
            opacity={spring({ frame, fps, config: { damping: 20 } })}
          >
            {title}
          </text>
        )}

        {/* Grid */}
        {showGrid && (
          <g
            opacity={interpolate(frame, [0, 10], [0, 0.5], {
              extrapolateRight: "clamp",
            })}
          >
            {/* Horizontal grid */}
            {gridLinesY.map((line, i) => (
              <g key={`gy-${i}`}>
                <line
                  x1={chartLeft}
                  y1={line.y}
                  x2={chartRight}
                  y2={line.y}
                  stroke={gridColor}
                  strokeWidth={1}
                />
                <text
                  x={chartLeft - 14}
                  y={line.y + 6}
                  textAnchor="end"
                  fill={textColor}
                  fontFamily={fontFamily}
                  fontSize={18}
                  fontWeight={400}
                >
                  {formatNumber(line.value)}
                </text>
              </g>
            ))}
            {/* Vertical grid */}
            {gridLinesX.map((line, i) => (
              <g key={`gx-${i}`}>
                <line
                  x1={line.x}
                  y1={chartTop}
                  x2={line.x}
                  y2={chartBottom}
                  stroke={gridColor}
                  strokeWidth={1}
                />
                <text
                  x={line.x}
                  y={chartBottom + 36}
                  textAnchor="middle"
                  fill={textColor}
                  fontFamily={fontFamily}
                  fontSize={18}
                  fontWeight={400}
                >
                  {formatNumber(line.value)}
                </text>
              </g>
            ))}
          </g>
        )}

        {/* Axes */}
        <line
          x1={chartLeft}
          y1={chartTop}
          x2={chartLeft}
          y2={chartBottom}
          stroke={gridColor}
          strokeWidth={2}
          opacity={interpolate(frame, [0, 8], [0, 1], {
            extrapolateRight: "clamp",
          })}
        />
        <line
          x1={chartLeft}
          y1={chartBottom}
          x2={chartRight}
          y2={chartBottom}
          stroke={gridColor}
          strokeWidth={2}
          opacity={interpolate(frame, [0, 8], [0, 1], {
            extrapolateRight: "clamp",
          })}
        />

        {/* Axis labels */}
        {xLabel && (
          <text
            x={chartLeft + chartWidth / 2}
            y={chartBottom + 70}
            textAnchor="middle"
            fill={textColor}
            fontFamily={fontFamily}
            fontSize={22}
            fontWeight={500}
            opacity={interpolate(frame, [5, 15], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            })}
          >
            {xLabel}
          </text>
        )}
        {yLabel && (
          <text
            x={40}
            y={chartTop + chartHeight / 2}
            textAnchor="middle"
            fill={textColor}
            fontFamily={fontFamily}
            fontSize={22}
            fontWeight={500}
            transform={`rotate(-90, 40, ${chartTop + chartHeight / 2})`}
            opacity={interpolate(frame, [5, 15], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            })}
          >
            {yLabel}
          </text>
        )}

        {/* Series lines */}
        {series.map((s, seriesIdx) => {
          const color = s.color || colors[seriesIdx % colors.length];
          const sorted = [...s.data].sort((a, b) => a.x - b.x);
          if (sorted.length < 2) return null;

          const pathD = sorted
            .map((p, i) => {
              const sx = toSvgX(p.x);
              const sy = toSvgY(p.y);
              return i === 0 ? `M ${sx} ${sy}` : `L ${sx} ${sy}`;
            })
            .join(" ");

          // Approximate path length for dash animation
          let pathLength = 0;
          for (let i = 1; i < sorted.length; i++) {
            const dx = toSvgX(sorted[i].x) - toSvgX(sorted[i - 1].x);
            const dy = toSvgY(sorted[i].y) - toSvgY(sorted[i - 1].y);
            pathLength += Math.sqrt(dx * dx + dy * dy);
          }

          const staggerDelay = seriesIdx * 8;

          let drawProgress: number;
          let lineOpacity: number;

          if (animationStyle === "draw") {
            drawProgress = spring({
              frame: frame - staggerDelay - 8,
              fps,
              config: { damping: 20, stiffness: 40 },
            });
            lineOpacity = interpolate(
              frame,
              [staggerDelay + 5, staggerDelay + 10],
              [0, 1],
              { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
            );
          } else {
            // fade-in
            drawProgress = 1;
            lineOpacity = spring({
              frame: frame - staggerDelay - 5,
              fps,
              config: { damping: 20 },
            });
          }

          const dashOffset = pathLength * (1 - drawProgress);

          return (
            <g key={s.label} opacity={fadeOut}>
              {/* Line */}
              <path
                d={pathD}
                fill="none"
                stroke={color}
                strokeWidth={strokeWidth}
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeDasharray={pathLength}
                strokeDashoffset={dashOffset}
                opacity={lineOpacity}
              />

              {/* Markers */}
              {showMarkers &&
                sorted.map((p, pIdx) => {
                  const markerProgress = interpolate(
                    drawProgress,
                    [pIdx / sorted.length, Math.min((pIdx + 1) / sorted.length, 1)],
                    [0, 1],
                    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
                  );
                  return (
                    <circle
                      key={`${s.label}-p-${pIdx}`}
                      cx={toSvgX(p.x)}
                      cy={toSvgY(p.y)}
                      r={5}
                      fill={backgroundColor}
                      stroke={color}
                      strokeWidth={2.5}
                      opacity={markerProgress * lineOpacity}
                    />
                  );
                })}
            </g>
          );
        })}

        {/* Legend */}
        {legendVisible && (
          <g
            opacity={interpolate(frame, [15, 25], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            })}
          >
            {series.map((s, i) => {
              const color = s.color || colors[i % colors.length];
              const legendX = centerX - (series.length * 160) / 2 + i * 160;
              return (
                <g key={`legend-${i}`}>
                  <rect
                    x={legendX}
                    y={legendY}
                    width={24}
                    height={4}
                    rx={2}
                    fill={color}
                  />
                  <text
                    x={legendX + 32}
                    y={legendY + 6}
                    fill={textColor}
                    fontFamily={fontFamily}
                    fontSize={20}
                    fontWeight={500}
                  >
                    {s.label}
                  </text>
                </g>
              );
            })}
          </g>
        )}
      </svg>
    </AbsoluteFill>
  );
};

function formatNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(1);
}
