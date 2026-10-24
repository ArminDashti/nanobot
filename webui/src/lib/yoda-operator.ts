import type { BootstrapResponse } from "@/lib/types";
import type { SettingsSectionKey } from "@/components/settings/contracts";

/** Yoda Postgres users with `admin=false` get a reduced Hawking UI. */
export function bootstrapYodaOperator(boot: BootstrapResponse): boolean {
  return boot.yoda?.operator !== false;
}

export function bootstrapYodaFirstName(boot: BootstrapResponse): string | null {
  const name = boot.yoda?.firstName?.trim();
  return name || null;
}

const OPERATOR_ONLY_SECTIONS: SettingsSectionKey[] = [
  "overview",
  "capabilities",
  "image",
  "voice",
  "browser",
  "memory",
  "runtime",
  "advanced",
  "skills",
];

export function isOperatorOnlySettingsSection(section: SettingsSectionKey): boolean {
  return OPERATOR_ONLY_SECTIONS.includes(section);
}

export function defaultSettingsSection(operator: boolean): SettingsSectionKey {
  return operator ? "overview" : "appearance";
}

export function coerceSettingsSection(
  section: SettingsSectionKey,
  operator: boolean,
): SettingsSectionKey {
  if (operator || !isOperatorOnlySettingsSection(section)) {
    return section;
  }
  return "appearance";
}

export function isOperatorOnlyShellView(
  view: string,
  settingsSection: SettingsSectionKey,
  operator: boolean,
): boolean {
  if (operator) return false;
  if (view === "skills" || view === "apps" || view === "channels") return true;
  if (view === "settings" && isOperatorOnlySettingsSection(settingsSection)) {
    return true;
  }
  return false;
}
