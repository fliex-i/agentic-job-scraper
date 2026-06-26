import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Search, RefreshCw, FileText } from "lucide-react";
import api, { type JobApplyRecord } from "@/services/api";
import { useToast } from "@/components/Layout";

const statusClassMap: Record<string, string> = {
  success: "bg-green-100 text-green-800 border-green-200",
  failed: "bg-red-100 text-red-800 border-red-200",
  skipped: "bg-yellow-100 text-yellow-800 border-yellow-200",
  dry_run: "bg-blue-100 text-blue-800 border-blue-200",
};

const formatTime = (value?: string) => {
  if (!value) return "-";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value;
  return dt.toLocaleString();
};

const ApplyRecords = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [records, setRecords] = useState<JobApplyRecord[]>([]);
  const [search, setSearch] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const limit = 20;

  useEffect(() => {
    loadRecords();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset, search]);

  const loadRecords = async () => {
    try {
      setLoading(true);
      const data = await api.getJobApplyRecords({ limit, offset });
      let rows = data.records || [];
      if (search.trim()) {
        const kw = search.toLowerCase();
        rows = rows.filter((r) => {
          return [
            r.status,
            r.reason,
            r.site,
            r.job?.title,
            r.job?.company,
            r.job_url,
          ]
            .filter(Boolean)
            .some((v) => String(v).toLowerCase().includes(kw));
        });
      }
      setRecords(rows);
      setTotal(data.total || 0);
    } catch (e: any) {
      showToast(
        "error",
        `${t("common.failedToLoad")} ${t("applyRecords.title")}: ${e.message || t("common.unknown")}`,
      );
    } finally {
      setLoading(false);
    }
  };

  const onSearch = () => {
    setOffset(0);
    setSearch(searchInput.trim());
  };

  const handleNext = () => setOffset((v) => v + limit);
  const handlePrevious = () => setOffset((v) => Math.max(0, v - limit));

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-4">
          <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <CardTitle className="flex items-center gap-2">
              <FileText size={18} />
              {t("applyRecords.title")}
            </CardTitle>
            <div className="flex items-center gap-2">
              <Input
                placeholder={t("applyRecords.searchPlaceholder")}
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                className="w-64"
              />
              <Button variant="outline" onClick={onSearch}>
                <Search size={16} className="mr-2" />
                {t("common.search")}
              </Button>
              <Button
                variant="outline"
                onClick={loadRecords}
                disabled={loading}
              >
                <RefreshCw
                  size={16}
                  className={`mr-2 ${loading ? "animate-spin" : ""}`}
                />
                {t("common.refresh")}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {records.length === 0 ? (
            <div className="text-sm text-muted-foreground py-8 text-center">
              {loading ? t("common.loading") : t("common.noData")}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-muted-foreground">
                    <th className="py-2 pr-3">{t("applyRecords.time")}</th>
                    <th className="py-2 pr-3">{t("applyRecords.status")}</th>
                    <th className="py-2 pr-3">{t("applyRecords.site")}</th>
                    <th className="py-2 pr-3">{t("applyRecords.job")}</th>
                    <th className="py-2 pr-3">{t("applyRecords.jobUrl")}</th>
                    <th className="py-2 pr-3">{t("applyRecords.reason")}</th>
                    <th className="py-2 pr-3">{t("applyRecords.resume")}</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((r) => (
                    <tr
                      key={r.id}
                      className="border-b last:border-b-0 align-top"
                    >
                      <td className="py-2 pr-3 whitespace-nowrap">
                        {formatTime(r.created_at)}
                      </td>
                      <td className="py-2 pr-3">
                        <Badge className={statusClassMap[r.status] || ""}>
                          {r.status}
                        </Badge>
                      </td>
                      <td className="py-2 pr-3">{r.site || "-"}</td>
                      <td className="py-2 pr-3">
                        <div className="font-medium">
                          {r.job?.title || `#${r.job_id}`}
                        </div>
                        <div className="text-muted-foreground">
                          {r.job?.company || "-"}
                        </div>
                      </td>
                      <td className="py-2 pr-3 max-w-80 break-all">
                        {(() => {
                          const url =
                            r.job_real_url || r.job?.real_url || r.job_url;
                          if (!url) return "-";
                          return (
                            <a
                              href={url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-blue-600 hover:underline"
                            >
                              {url}
                            </a>
                          );
                        })()}
                      </td>
                      <td className="py-2 pr-3 max-w-80 break-all">
                        {r.reason || "-"}
                      </td>
                      <td className="py-2 pr-3 whitespace-nowrap">
                        {r.resume_language || "-"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="flex items-center justify-between mt-4 text-sm">
            <div className="text-muted-foreground">
              {t("common.page")} {Math.floor(offset / limit) + 1} /{" "}
              {Math.max(1, Math.ceil(total / limit))}
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handlePrevious}
                disabled={offset === 0}
              >
                {t("common.previous")}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={handleNext}
                disabled={offset + limit >= total}
              >
                {t("common.next")}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

export default ApplyRecords;
