return {
  name = "pfp-authz",
  fields = {
    {
      config = {
        type = "record",
        fields = {
          {
            authz_url = {
              type = "string",
              required = true,
            },
          },
          {
            shared_secret = {
              type = "string",
              required = true,
              referenceable = true,
            },
          },
          {
            timeout_ms = {
              type = "integer",
              default = 1000,
              between = {100, 5000},
            },
          },
        },
      },
    },
  },
}
