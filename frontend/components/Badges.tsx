import { InfoTip } from "@/components/ui/InfoTip";
import { Badge } from "@/components/ui/primitives";
import { DATA_CLASS, REC_STATUS, STATEMENT } from "@/lib/labels";
import type { DataClass, RecStatus, StatementLabel } from "@/lib/types";

export function DataClassBadge({ value }: { value: DataClass }) {
  const info = DATA_CLASS[value];
  const tone =
    value === "SYNTHETIC" ? "synthetic" : value === "MISSING" ? "neutral" : value === "FORECAST" ? "warning" : "info";
  return (
    <InfoTip content={info.description}>
      <Badge tone={tone}>{info.label}</Badge>
    </InfoTip>
  );
}

export function RecStatusBadge({ value }: { value: RecStatus }) {
  const info = REC_STATUS[value];
  return (
    <InfoTip content={info.description}>
      <Badge tone={info.tone} glyph={info.glyph}>
        {info.label}
      </Badge>
    </InfoTip>
  );
}

export function StatementBadge({ value }: { value: StatementLabel }) {
  const info = STATEMENT[value];
  const tone = value === "VERIFIED_FACT" ? "good" : value === "UNKNOWN" ? "critical" : value === "HYPOTHESIS_TO_TEST" ? "warning" : "info";
  return (
    <InfoTip content={info.description}>
      <Badge tone={tone} glyph={info.glyph}>
        {info.label}
      </Badge>
    </InfoTip>
  );
}

export function SyntheticBadge() {
  return (
    <InfoTip content={DATA_CLASS.SYNTHETIC.description}>
      <Badge tone="synthetic">Synthetic</Badge>
    </InfoTip>
  );
}
