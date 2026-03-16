"""GraphQL query strings for the Coles API."""

_GQL_PRODUCT_FIELDS = """
    id
    name
    brand
    description
    imageUris { uri }
    size
    pricing {
        now
        was
        unit { price }
        promotionType
        saveAmount
    }
"""

GQL_SEARCH = """
query SearchProducts(
    $searchTerm: String!,
    $storeId: BrandedId!,
    $pageNumber: Int = 1,
    $pageSize: Int = 48
) {
    searchProducts(input: {
        searchTerm: $searchTerm
        storeId: $storeId
        pagination: { pageNumber: $pageNumber pageSize: $pageSize }
    }) {
        results {
            """ + _GQL_PRODUCT_FIELDS + """
        }
    }
}
"""

GQL_CROSS_CATEGORY = """
query GetCrossCategory(
    $categoryIds: [ID!]!,
    $storeId: BrandedId!,
    $memoryToken: String
) {
    crossCategory(
        categoryIds: $categoryIds
        storeId: $storeId
        memoryToken: $memoryToken
    ) {
        products {
            """ + _GQL_PRODUCT_FIELDS + """
        }
        memoryToken
    }
}
"""
