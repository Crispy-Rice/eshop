import { get } from './http'

/** 库存展示档位。**后端不回传真实库存**，只给取整后的档位（docs/03 §9）。 */
export interface SkuStockDisplay {
  skuId: string
  /** 有货 / 仅剩 N 件以内 / 已售罄 */
  text: string
  soldOut: boolean
}

export function fetchSkuStock(skuId: string): Promise<SkuStockDisplay> {
  return get<SkuStockDisplay>(`/skus/${skuId}/stock`)
}
