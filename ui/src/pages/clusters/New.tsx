import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { createCluster } from "@/api/resources";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { NameFields, isNameFieldError, nameFieldErrors, type NameValue } from "@/components/NameFields";
import { PageWidth } from "@/components/PageWidth";
import { Button, buttonVariants } from "@/components/ui/button";
import { Alert, Card, CardContent } from "@/components/ui/card";
import { Field, Input, Textarea } from "@/components/ui/input";
import { useT } from "@/i18n";
import { readFileText } from "@/lib/utils";

import { useClusterWrite } from "./permission";

export function ClusterNewPage() {
  const t = useT();
  const text = t.clusters.form;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { ready, canWrite } = useClusterWrite();
  const [names, setNames] = useState<NameValue>({ displayName: "", name: "" });
  const [platformUrl, setPlatformUrl] = useState("");
  const [kubeconfig, setKubeconfig] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      createCluster({
        name: names.name.trim(),
        display_name: names.displayName.trim() || undefined,
        connection: { kubeconfig },
        platform_internal_url: platformUrl.trim(),
        images: { pull_policy: "IfNotPresent" },
        namespaces: { labels: {} },
        ingress: { enabled: false, allow_unauthenticated: false, tls: "none" },
        scheduling: {},
      }),
    onSuccess: async (c) => {
      await queryClient.invalidateQueries({ queryKey: ["clusters"] });
      navigate(`/clusters/${c.id}`);
    },
  });

  async function onFile(f: File | null) {
    if (!f) return;
    // 이름을 먼저 넣으면 "loaded X" 는 보이는데 본문은 아직 빈 순간이 생긴다. 읽고 나서 함께 반영한다
    const text = await readFileText(f);
    setFileName(f.name);
    setKubeconfig(text);
  }

  // 주소창으로 들어올 수 있는 화면이다. kubeconfig 를 다 붙여 넣고서야 403 을 만나지 않도록 먼저 막는다
  if (!ready) return <p className="text-sm text-muted">{t.common.loading}</p>;
  if (!canWrite) {
    return (
      <PageWidth data-testid="cluster-new-page">
        <PageHeader title={text.title} testId="page-title" />
        <Card className="max-w-2xl">
          <CardContent className="flex flex-col items-start gap-3">
            <Alert variant="info" data-testid="readonly-notice">
              {t.clusters.readOnly.notice}
            </Alert>
            <Link to="/clusters" className={buttonVariants({ variant: "outline" })}>
              {text.backToList}
            </Link>
          </CardContent>
        </Card>
      </PageWidth>
    );
  }

  return (
    <PageWidth data-testid="cluster-new-page">
      <PageHeader title={text.title} testId="page-title" />
      <Card className="max-w-2xl">
        <CardContent>
          <form
            className="flex flex-col gap-3"
            data-testid="cluster-form"
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate();
            }}
          >
            <NameFields testId="cl" placeholders={text.placeholders} value={names} onChange={setNames} nameHint={text.nameHint} errors={nameFieldErrors(create.error)} />
            <Field label={text.platformUrl} htmlFor="cl-platform-url" hint={text.platformUrlHint}>
              <Input
                id="cl-platform-url"
                data-testid="cl-platform-url"
                required
                placeholder="http://daegam-platform.daegam-system.svc:9000"
                value={platformUrl}
                onChange={(e) => setPlatformUrl(e.target.value)}
              />
            </Field>
            <Field label={text.kubeconfigFile} hint={text.kubeconfigFileHint} htmlFor="cl-kubeconfig-file">
              <Input
                id="cl-kubeconfig-file"
                data-testid="cl-kubeconfig-file"
                type="file"
                onChange={(e) => void onFile(e.target.files?.[0] ?? null)}
              />
              {fileName ? <p className="text-xs text-muted">{text.loaded(fileName)}</p> : null}
            </Field>
            <Field label={text.kubeconfig} htmlFor="cl-kubeconfig" hint={text.kubeconfigHint}>
              <Textarea
                id="cl-kubeconfig"
                data-testid="cl-kubeconfig"
                rows={12}
                required
                value={kubeconfig}
                onChange={(e) => setKubeconfig(e.target.value)}
              />
            </Field>
            {!isNameFieldError(create.error) ? <ErrorMessage error={create.error} testId="save-error" /> : null}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => navigate("/clusters")}>
                {t.common.cancel}
              </Button>
              <Button type="submit" data-testid="save-button" disabled={create.isPending || !kubeconfig.trim()}>
                {create.isPending ? text.registering : text.register}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </PageWidth>
  );
}
