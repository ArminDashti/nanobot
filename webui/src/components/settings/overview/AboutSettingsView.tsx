import type { ReactNode } from "react";
import { BookOpen, ExternalLink, Github, Linkedin, MessageCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { SettingsGroup } from "@/components/settings/shared/SettingsControls";

export function AboutSettingsView({ reportIssueUrl, versionCheck, starLink }: {
  reportIssueUrl: string;
  versionCheck: ReactNode;
  starLink: ReactNode;
}) {
  const { t } = useTranslation();
  const creatorLinks = [
    { key: "linkedin" as const, icon: Linkedin, href: "https://www.linkedin.com/in/armin-dashti-058239a1/" },
    { key: "github" as const, icon: Github, href: "https://github.com/armindashti" },
  ];
  const links = [
    { key: "sourceCode", icon: Github, href: "https://github.com/HKUDS/nanobot" },
    { key: "documentation", icon: BookOpen, href: "https://nanobot.wiki/" },
    { key: "reportIssue", icon: MessageCircle, href: reportIssueUrl },
  ];
  return (
    <div className="flex flex-1 flex-col gap-5">
      <div className="flex flex-col items-center gap-4 py-6 text-center">
        <img src="/brand/hawking.jpg" alt="" className="h-16 w-16 select-none rounded-full object-cover" draggable={false} />
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">Hawking</h1>
        <div className="flex items-center justify-center gap-4">
          {creatorLinks.map(({ key, icon: Icon, href }) => (
            <a
              key={key}
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={t(`settings.about.${key}`)}
              className="rounded-full p-2 text-muted-foreground transition-colors settings-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Icon className="h-6 w-6" aria-hidden />
            </a>
          ))}
        </div>
        <div className="max-w-md space-y-3 px-2 text-sm leading-relaxed text-muted-foreground">
          <p>{t("settings.about.creatorIntroEn")}</p>
          <p dir="rtl">{t("settings.about.creatorIntroFa")}</p>
          <p className="border-t border-border/60 pt-3 text-xs leading-relaxed">
            {t("settings.about.frameworkNoteEn")}
          </p>
          <p dir="rtl" className="text-xs leading-relaxed">
            {t("settings.about.frameworkNoteFa")}
          </p>
        </div>
        {versionCheck}
      </div>
      <SettingsGroup>
        {links.map(({ key, icon: Icon, href }) => (
          <a key={key} href={href} target="_blank" rel="noopener noreferrer"
            className="settings-list-row flex select-none items-center gap-3 text-[14px] settings-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <Icon className="h-4 w-4 text-muted-foreground" aria-hidden />
            <span className="flex-1">{t(`settings.about.${key}`)}</span>
            <ExternalLink className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
          </a>
        ))}
      </SettingsGroup>
      <div className="mt-auto pt-8 text-center">
        {starLink}
      </div>
    </div>
  );
}
