import { ReactNode } from "react";

/**
 * The section cut — a vertical slice through the ground beneath a street.
 *
 * This is the product's argument in one drawing: the complaint happens at the
 * surface, the cause is 1.5m down. Every mark is driven by a real Mireye
 * field, so it reads as an instrument rather than an illustration:
 *
 *   shrink_swell_class  -> desiccation cracks (count + depth)
 *   bedrock_depth_cm    -> where the C-horizon terminates
 *   drainage_class      -> water table position
 *   trigger_state       -> the wetting front, and which way it is moving
 *   soil_usable = false -> an explicit NO READING zone, never a guess
 *
 * That last one matters most: on `Urban land` the honest output is a refusal,
 * so the drawing must be able to show an absence of knowledge as clearly as
 * it shows knowledge.
 */

const SURFACE_Y = 54;
const BOTTOM_Y = 272;
const MAX_DEPTH_CM = 250;
const PX_PER_CM = (BOTTOM_Y - SURFACE_Y) / MAX_DEPTH_CM;

const depthToY = (cm: number) => SURFACE_Y + cm * PX_PER_CM;

const MAIN_DEPTH_CM = 150; // typical Houston distribution main burial depth

type ShrinkSwell = "Low" | "Moderate" | "High" | "Very High" | string;

/** Crack geometry by shrink-swell severity. Vertisol desiccation cracks
 *  propagate from the surface downward, so depth encodes severity. */
const CRACKS: Record<string, { x: number; depthCm: number }[]> = {
  "Very High": [
    { x: 96, depthCm: 128 },
    { x: 208, depthCm: 96 },
    { x: 318, depthCm: 140 },
    { x: 452, depthCm: 104 },
    { x: 556, depthCm: 120 },
  ],
  High: [
    { x: 128, depthCm: 88 },
    { x: 286, depthCm: 104 },
    { x: 470, depthCm: 76 },
  ],
  Moderate: [
    { x: 168, depthCm: 52 },
    { x: 402, depthCm: 44 },
  ],
  Low: [],
};

/** Deterministic jag — no Math.random, so server and client render identically. */
function crackPath(x: number, depthCm: number, seed: number) {
  const endY = depthToY(depthCm);
  const steps = 5;
  const drift = [0, 3, -2, 4, -3, 2];
  let d = `M ${x} ${SURFACE_Y}`;
  for (let i = 1; i <= steps; i++) {
    const y = SURFACE_Y + ((endY - SURFACE_Y) * i) / steps;
    const dx = x + drift[(i + seed) % drift.length] * (1 - i / (steps + 1));
    d += ` L ${dx.toFixed(1)} ${y.toFixed(1)}`;
  }
  return d;
}

const WATER_TABLE_BY_DRAINAGE: Record<string, number> = {
  "Very poorly drained": 40,
  "Poorly drained": 62,
  "Somewhat poorly drained": 88,
  "Moderately well drained": 132,
  "Well drained": 178,
  "Somewhat excessively drained": 205,
  "Excessively drained": 222,
};

const TRIGGER_COPY: Record<string, { label: string; note: string }> = {
  rewetting: { label: "Rewetting", note: "Clay is taking water back on — swelling against the main" },
  drying: { label: "Drying", note: "Clay is contracting — soil pulls away, cracks open" },
  sustained_dry: { label: "Sustained dry", note: "Cracks fully open, ground at maximum contraction" },
  stable: { label: "Stable", note: "No active movement in the clay right now" },
};

export interface SectionCutProps {
  shrinkSwell?: ShrinkSwell | null;
  drainage?: string | null;
  mapUnit?: string | null;
  bedrockCm?: number | null;
  soilUsable?: boolean | null;
  triggerState?: string | null;
  /** Marks a complaint at the surface. */
  complaintLabel?: string | null;
  animate?: boolean;
  className?: string;
  caption?: ReactNode;
}

export function SectionCut({
  shrinkSwell,
  drainage,
  mapUnit,
  bedrockCm,
  soilUsable,
  triggerState,
  complaintLabel,
  animate = true,
  className = "",
  caption,
}: SectionCutProps) {
  const usable = soilUsable !== false && soilUsable !== null && soilUsable !== undefined;
  const cracks = usable ? CRACKS[shrinkSwell ?? ""] ?? [] : [];
  const waterTableY = drainage ? depthToY(WATER_TABLE_BY_DRAINAGE[drainage] ?? 140) : null;
  const mainY = depthToY(MAIN_DEPTH_CM);
  const bedrockY =
    bedrockCm != null && bedrockCm <= MAX_DEPTH_CM ? depthToY(bedrockCm) : null;
  const trigger = triggerState ? TRIGGER_COPY[triggerState] : null;

  const anim = (delay: number) =>
    animate
      ? { animation: `strata-in 620ms cubic-bezier(0.16,1,0.3,1) ${delay}ms both`, transformOrigin: "top" }
      : undefined;

  return (
    <figure className={`w-full ${className}`}>
      <svg
        viewBox="0 0 640 300"
        className="w-full h-auto"
        role="img"
        aria-label={
          usable
            ? `Cross-section of the ground beneath the street: ${mapUnit ?? "soil"}, ${
                shrinkSwell ?? "unknown"
              } shrink-swell potential, water main at ${MAIN_DEPTH_CM} centimetres depth.`
            : "Cross-section placeholder: the dominant soil component here is Urban land, so no subsurface reading is available."
        }
      >
        <defs>
          <pattern id="noread" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="8" stroke="var(--bone-faint)" strokeWidth="1" opacity="0.45" />
          </pattern>
          <linearGradient id="wet" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--moisture)" stopOpacity="0.34" />
            <stop offset="100%" stopColor="var(--moisture)" stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* depth axis — the borehole-log vernacular */}
        {[0, 50, 100, 150, 200, 250].map((cm) => (
          <g key={cm}>
            <line
              x1="42"
              y1={depthToY(cm)}
              x2="48"
              y2={depthToY(cm)}
              stroke="var(--bone-faint)"
              strokeWidth="1"
            />
            <text
              x="36"
              y={depthToY(cm) + 3.5}
              textAnchor="end"
              className="data"
              fontSize="9"
              fill="var(--bone-faint)"
            >
              {cm === 0 ? "0" : `−${cm}`}
            </text>
          </g>
        ))}
        <text x="6" y="20" className="data" fontSize="9" fill="var(--bone-faint)">
          cm
        </text>

        {/* sky / above-grade */}
        <rect x="50" y="10" width="580" height={SURFACE_Y - 10} fill="var(--ground-850)" />

        {usable ? (
          <>
            {/* A horizon */}
            <rect
              x="50" y={SURFACE_Y} width="580" height={depthToY(45) - SURFACE_Y}
              fill="var(--clay-light)" opacity="0.85" style={anim(80)}
            />
            {/* B horizon — the Vertisol clay, the one that moves */}
            <rect
              x="50" y={depthToY(45)} width="580" height={depthToY(bedrockCm != null && bedrockCm < 190 ? bedrockCm : 190) - depthToY(45)}
              fill="var(--clay)" opacity="0.92" style={anim(200)}
            />
            {/* C horizon */}
            {(bedrockY == null || bedrockCm! > 190) && (
              <rect
                x="50" y={depthToY(190)} width="580" height={BOTTOM_Y - depthToY(190)}
                fill="var(--clay-deep)" opacity="0.9" style={anim(320)}
              />
            )}
            {bedrockY != null && (
              <>
                <rect
                  x="50" y={bedrockY} width="580" height={BOTTOM_Y - bedrockY}
                  fill="var(--ground-700)" style={anim(320)}
                />
                <line x1="50" y1={bedrockY} x2="630" y2={bedrockY} stroke="var(--bone-faint)" strokeWidth="1.5" strokeDasharray="4 3" />
                <text x="624" y={bedrockY + 14} textAnchor="end" className="data" fontSize="9" fill="var(--bone-dim)">
                  bedrock −{bedrockCm}cm
                </text>
              </>
            )}

            {/* wetting front */}
            {waterTableY != null && (
              <>
                <rect x="50" y={waterTableY} width="580" height={BOTTOM_Y - waterTableY} fill="url(#wet)" />
                <line
                  x1="50" y1={waterTableY} x2="630" y2={waterTableY}
                  stroke="var(--moisture)" strokeWidth="1.5" strokeDasharray="6 4" opacity="0.85"
                />
              </>
            )}

            {/* desiccation cracks — severity read straight off shrink_swell_class */}
            {cracks.map((c, i) => (
              <path
                key={c.x}
                d={crackPath(c.x, c.depthCm, i)}
                stroke="var(--ground-900)"
                strokeWidth="2.4"
                fill="none"
                strokeLinecap="round"
                opacity="0.75"
                style={
                  animate
                    ? {
                        strokeDasharray: 200,
                        strokeDashoffset: 200,
                        animation: `draw 900ms ease-out ${520 + i * 90}ms both`,
                      }
                    : undefined
                }
              />
            ))}
          </>
        ) : (
          /* Honest absence: the gate that stops the agents inventing soil. */
          <>
            <rect x="50" y={SURFACE_Y} width="580" height={BOTTOM_Y - SURFACE_Y} fill="var(--ground-850)" style={anim(80)} />
            <rect x="50" y={SURFACE_Y} width="580" height={BOTTOM_Y - SURFACE_Y} fill="url(#noread)" style={anim(80)} />
            <text x="340" y={depthToY(120)} textAnchor="middle" className="data" fontSize="12" fill="var(--bone-dim)">
              NO SUBSURFACE READING
            </text>
            <text x="340" y={depthToY(148)} textAnchor="middle" className="data" fontSize="9.5" fill="var(--bone-faint)">
              dominant component is Urban land
            </text>
          </>
        )}

        {/* street surface */}
        <line x1="50" y1={SURFACE_Y} x2="630" y2={SURFACE_Y} stroke="var(--bone)" strokeWidth="2" />
        <rect x="50" y={SURFACE_Y - 5} width="580" height="5" fill="var(--ground-700)" />

        {/* the main, at depth */}
        <g style={animate ? { animation: `rise 500ms ease-out 760ms both` } : undefined}>
          <circle cx="436" cy={mainY} r="11" fill="var(--ground-900)" stroke="var(--oxide)" strokeWidth="2.5" />
          <circle cx="436" cy={mainY} r="4" fill="var(--oxide)" opacity="0.55" />
          <line x1="50" y1={mainY} x2="425" y2={mainY} stroke="var(--oxide)" strokeWidth="2" opacity="0.32" strokeDasharray="3 5" />
          <line x1="447" y1={mainY} x2="630" y2={mainY} stroke="var(--oxide)" strokeWidth="2" opacity="0.32" strokeDasharray="3 5" />
          {/* Backing plate: oxide-bright on clay is ~1.6:1, unreadable. The
              plate restores it to the same contrast the label has elsewhere. */}
          <rect x="452" y={mainY - 25} width="76" height="14" rx="2" fill="var(--ground-900)" opacity="0.88" />
          <text x="458" y={mainY - 15} className="data" fontSize="9.5" fill="var(--oxide-bright)">
            main −{MAIN_DEPTH_CM}cm
          </text>
        </g>

        {/* the complaint — surface symptom of all of the above */}
        {complaintLabel && (
          <g style={animate ? { animation: `pin-drop 600ms cubic-bezier(0.34,1.4,0.5,1) 900ms both` } : undefined}>
            <line x1="436" y1={SURFACE_Y - 34} x2="436" y2={SURFACE_Y} stroke="var(--bone)" strokeWidth="1.5" />
            <circle cx="436" cy={SURFACE_Y - 38} r="5.5" fill="var(--bone)" />
            <text x="448" y={SURFACE_Y - 34} className="data" fontSize="10" fill="var(--bone)">
              {complaintLabel}
            </text>
          </g>
        )}
      </svg>

      {(trigger || caption) && (
        <figcaption className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
          {trigger && (
            <span className="inline-flex items-center gap-2">
              <span className="h-2 w-2 rounded-full bg-moisture" aria-hidden="true" />
              <span className="data text-moisture">{trigger.label}</span>
              <span className="text-bone-dim">{trigger.note}</span>
            </span>
          )}
          {caption}
        </figcaption>
      )}
    </figure>
  );
}
