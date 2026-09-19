# Hugo MCP 工具合同

本文件由 `scripts/generate_tools_docs.py` 从 `server.py` 的 `TOOLS` 注册表生成。
实际工具列表、参数 Schema 与本文保持一致；服务只提供这 8 个工具。

## `hugo_list_posts`

List Hugo posts and their revisions.

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

Read one Hugo Markdown post.

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

Create a draft Markdown post. Requires request_id.

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
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_update_draft`

Update a post with optimistic revision checking.

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
    "request_id": {
      "type": "string"
    }
  }
}
```

## `hugo_publish_post`

Publish a post, rebuild Hugo, and return the new revision.

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

Soft-delete a post into the private Hugo MCP trash and rebuild Hugo.

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

## `hugo_build`

Build the Hugo site from the current Markdown source.

### 输入 Schema

```json
{
  "type": "object",
  "properties": {}
}
```
