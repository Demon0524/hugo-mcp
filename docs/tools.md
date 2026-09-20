# Hugo MCP 工具合同

本文件由 `scripts/generate_tools_docs.py` 从 `server.py` 的 `TOOLS` 注册表生成。
实际工具列表、参数 Schema 与本文保持一致；当前服务提供 12 个工具。

## `hugo_list_posts`

List Hugo posts and their revisions. Supports flat Markdown and Page Bundles.

### 输入 Schema

```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": [
        "all",
        "draft",
        "publish"
      ]
    },
    "query": {
      "type": "string"
    },
    "limit": {
      "type": "integer"
    },
    "offset": {
      "type": "integer"
    }
  }
}
```

## `hugo_get_post`

Read one Hugo post, including Page Bundle media metadata when available.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "slug"
  ],
  "properties": {
    "slug": {
      "type": "string"
    }
  }
}
```

## `hugo_create_draft`

Create a draft post. New drafts use a Hugo Page Bundle by default. Requires request_id.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "title",
    "request_id"
  ],
  "properties": {
    "title": {
      "type": "string"
    },
    "slug": {
      "type": "string"
    },
    "content": {
      "type": "string"
    },
    "categories": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "tags": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "bundle": {
      "type": "boolean",
      "default": true
    },
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_update_draft`

Update a flat post or Page Bundle with optimistic revision checking.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "slug",
    "expected_revision",
    "request_id"
  ],
  "properties": {
    "slug": {
      "type": "string"
    },
    "expected_revision": {
      "type": "string"
    },
    "title": {
      "type": "string"
    },
    "content": {
      "type": "string"
    },
    "categories": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "tags": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "summary": {
      "type": "string"
    },
    "date": {
      "type": "string"
    },
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_publish_post`

Publish a flat post or Page Bundle, rebuild Hugo, and return the new revision.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "slug",
    "expected_revision",
    "request_id"
  ],
  "properties": {
    "slug": {
      "type": "string"
    },
    "expected_revision": {
      "type": "string"
    },
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_unpublish_post`

Move a published post back to draft and rebuild Hugo.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "slug",
    "expected_revision",
    "request_id"
  ],
  "properties": {
    "slug": {
      "type": "string"
    },
    "expected_revision": {
      "type": "string"
    },
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_delete_post`

Soft-delete a post or Page Bundle into the private Hugo MCP trash and rebuild Hugo.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "slug",
    "expected_revision",
    "request_id"
  ],
  "properties": {
    "slug": {
      "type": "string"
    },
    "expected_revision": {
      "type": "string"
    },
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_migrate_post_bundle`

Move one legacy flat post into content/posts/<slug>/index.md without changing its slug.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "slug",
    "expected_revision",
    "request_id"
  ],
  "properties": {
    "slug": {
      "type": "string"
    },
    "expected_revision": {
      "type": "string"
    },
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_upload_media`

Upload a validated image into a Page Bundle and return a relative Markdown reference.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "post_slug",
    "mime_type",
    "data_base64",
    "request_id"
  ],
  "properties": {
    "post_slug": {
      "type": "string"
    },
    "filename": {
      "type": "string"
    },
    "mime_type": {
      "type": "string",
      "enum": [
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif"
      ]
    },
    "data_base64": {
      "type": "string"
    },
    "alt": {
      "type": "string",
      "maxLength": 300
    },
    "role": {
      "type": "string",
      "enum": [
        "cover",
        "inline",
        "attachment"
      ],
      "default": "inline"
    },
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_list_media`

List image media stored beside a Page Bundle index.md.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "post_slug"
  ],
  "properties": {
    "post_slug": {
      "type": "string"
    }
  }
}
```

## `hugo_delete_media`

Soft-delete one Page Bundle media file after checking its media revision, then rebuild Hugo.

### 输入 Schema

```json
{
  "type": "object",
  "required": [
    "post_slug",
    "filename",
    "expected_revision",
    "request_id"
  ],
  "properties": {
    "post_slug": {
      "type": "string"
    },
    "filename": {
      "type": "string"
    },
    "expected_revision": {
      "type": "string"
    },
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_build`

Build the Hugo site from the current Markdown source and Page Bundles.

### 输入 Schema

```json
{
  "type": "object",
  "properties": {}
}
```
