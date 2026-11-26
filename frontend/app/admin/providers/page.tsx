"use client";

import { useState } from "react";
import { ProviderEditor, ProviderList, type ProviderOps } from "@/components/providers";
import { Button, Card, CardHeader, DataView, PageHeader } from "@/components/ui";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";

const ops: ProviderOps = {
  create: api.admin.createProvider,
  update: api.admin.updateProvider,
  remove: api.admin.deleteProvider,
  test: api.admin.testProvider,
};

export default function SharedProvidersPage() {
  const providers = useAsync(() => api.admin.providers(), "admin-providers");
  const [adding, setAdding] = useState(false);

  return (
    <>
      <PageHeader
        title="Shared AI providers"
        description="Used by every tenant that hasn't added its own key. The default provider is used first."
        actions={
          <Button variant="primary" onClick={() => setAdding(true)}>
            Add provider
          </Button>
        }
      />
      <Card>
        <CardHeader title="Providers" />
        <DataView state={providers} isEmpty={() => false}>
          {(list) => (
            <ProviderList
              providers={list}
              ops={ops}
              onChange={(next) => providers.setData(next)}
              emptyTitle="No shared providers"
              emptyDescription="Add one so tenants can use AI without their own key."
              emptyAction={
                <Button size="sm" variant="primary" onClick={() => setAdding(true)}>
                  Add provider
                </Button>
              }
            />
          )}
        </DataView>
      </Card>
      {adding && (
        <ProviderEditor
          provider={null}
          ops={ops}
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            providers.reload();
          }}
        />
      )}
    </>
  );
}
