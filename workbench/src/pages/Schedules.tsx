import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Empty, PageHeader, fmt } from "../components/ui";

export function Schedules() {
  const qc = useQueryClient();
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: api.listSchedules });
  const skills = useQuery({ queryKey: ["skills"], queryFn: api.listSkills });
  const [form, setForm] = useState({
    name: "",
    skill_slug: "incident-investigation",
    trigger_kind: "cron",
    cron_expr: "0 2 * * *",
  });

  const create = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        name: form.name,
        skill_slug: form.skill_slug,
        trigger_kind: form.trigger_kind,
      };
      if (form.trigger_kind === "cron") body.cron_expr = form.cron_expr;
      else body.event_filter = { match: {}, notify: { surface: "slack", channel: "" } };
      return api.createSchedule(body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["schedules"] });
      setForm({ ...form, name: "" });
    },
  });
  const toggle = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => api.patchSchedule(v.id, { enabled: v.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedules"] }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteSchedule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedules"] }),
  });

  return (
    <div>
      <PageHeader title="Schedules" sub="Cron sweeps and event-triggered investigations" />

      <div className="card mb-5 grid grid-cols-5 gap-2">
        <input className="input" placeholder="name" value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <select className="input" value={form.skill_slug}
          onChange={(e) => setForm({ ...form, skill_slug: e.target.value })}>
          {(skills.data ?? []).map((s) => <option key={s.slug} value={s.slug}>{s.slug}</option>)}
        </select>
        <select className="input" value={form.trigger_kind}
          onChange={(e) => setForm({ ...form, trigger_kind: e.target.value })}>
          <option value="cron">cron</option>
          <option value="event">event</option>
        </select>
        <input className="input" placeholder="cron expr" value={form.cron_expr}
          disabled={form.trigger_kind !== "cron"}
          onChange={(e) => setForm({ ...form, cron_expr: e.target.value })} />
        <button className="btn" disabled={!form.name || create.isPending}
          onClick={() => create.mutate()}>Add</button>
      </div>

      {schedules.data?.length === 0 ? (
        <Empty>No schedules.</Empty>
      ) : (
        <div className="card overflow-hidden p-0">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Name</th><th className="th">Trigger</th>
                <th className="th">Next run</th><th className="th">Enabled</th><th className="th"></th>
              </tr>
            </thead>
            <tbody>
              {(schedules.data ?? []).map((s) => (
                <tr key={s.id}>
                  <td className="td">{s.name}</td>
                  <td className="td text-muted">{s.trigger_kind} {s.cron_expr ?? ""}</td>
                  <td className="td text-muted">{fmt(s.next_run_at)}</td>
                  <td className="td">{s.enabled ? "yes" : "no"}</td>
                  <td className="td text-right">
                    <button className="btn mr-2"
                      onClick={() => toggle.mutate({ id: s.id, enabled: !s.enabled })}>
                      {s.enabled ? "Disable" : "Enable"}
                    </button>
                    <button className="btn" onClick={() => remove.mutate(s.id)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
