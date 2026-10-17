// Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
import type { OrganizationPrices } from "./organization-api";
import { catalogModel } from "./modelRecommendations";

export function availableEfforts(catalog: OrganizationPrices | null, model: string): string[] {
  return catalogModel(catalog, model)?.reasoning_efforts ?? [];
}
