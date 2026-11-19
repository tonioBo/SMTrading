import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { Campaign, CampaignCloseCheck, CampaignCreate, CampaignPortfolioSpec, CampaignTemplate } from "@/api/types";

export const campaignsKey = ["campaigns"] as const;

/** `GET /api/campaigns`: open campaigns first, then the concluded ones. */
export function useCampaigns() {
  return useQuery({ queryKey: campaignsKey, queryFn: () => apiFetch<Campaign[]>("/api/campaigns") });
}

/** `POST /api/campaigns/templates` (admin): the goal templates of the wizard's third step. */
export function useCampaignTemplates() {
  return useMutation({
    mutationFn: (body: { start_date: string; end_date: string; portfolios: CampaignPortfolioSpec[] }) =>
      apiFetch<CampaignTemplate[]>("/api/campaigns/templates", { method: "POST", body }),
  });
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: campaignsKey });
    void qc.invalidateQueries({ queryKey: ["portfolios"] });
    void qc.invalidateQueries({ queryKey: ["goals"] });
    void qc.invalidateQueries({ queryKey: ["broker-links"] });
  };
}

/** The wizard's confirm: campaign, portfolios, links and goals in one transaction. */
export function useCreateCampaign() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: CampaignCreate) => apiFetch<Campaign>("/api/campaigns", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function usePatchCampaign() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; notes?: string | null; status?: "attiva" } }) =>
      apiFetch<Campaign>(`/api/campaigns/${id}`, { method: "PATCH", body }),
    onSuccess: invalidate,
  });
}

/** What is still open before closing (archive blockers, open positions/orders, pianos). */
export function useCampaignCloseCheck(id: string) {
  return useQuery({
    queryKey: [...campaignsKey, id, "close-check"],
    queryFn: () => apiFetch<CampaignCloseCheck>(`/api/campaigns/${id}/close-check`),
  });
}

export function useCloseCampaign() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, archive, notes }: { id: string; archive: string[]; notes?: string }) =>
      apiFetch<Campaign>(`/api/campaigns/${id}/close`, {
        method: "POST",
        body: { archive_portfolio_ids: archive, ...(notes !== undefined ? { notes } : {}) },
      }),
    onSuccess: invalidate,
  });
}
