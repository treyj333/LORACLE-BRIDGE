"""Pack manifest schema and loader."""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PackDocument:
    id: str
    title: str
    category: str
    url: str
    filename: str
    license: str
    publisher: str
    size_bytes_estimate: int = 0
    sha256: Optional[str] = None
    required: bool = True
    attribution: Optional[str] = None


@dataclass
class PackManifest:
    id: str
    version: str
    name: str
    description: str
    categories: List[str]
    license_summary: str
    maintainer: str
    homepage: str
    estimated_size_mb: int
    estimated_install_time_seconds: int
    documents: List[PackDocument]

    @staticmethod
    def from_file(path: str) -> "PackManifest":
        with open(path) as f:
            data = json.load(f)
        docs = [PackDocument(**d) for d in data.get("documents", [])]
        return PackManifest(
            id=data["id"],
            version=data["version"],
            name=data["name"],
            description=data["description"],
            categories=data.get("categories", []),
            license_summary=data.get("license_summary", ""),
            maintainer=data.get("maintainer", ""),
            homepage=data.get("homepage", ""),
            estimated_size_mb=data.get("estimated_size_mb", 0),
            estimated_install_time_seconds=data.get("estimated_install_time_seconds", 0),
            documents=docs,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "categories": self.categories,
            "license_summary": self.license_summary,
            "maintainer": self.maintainer,
            "homepage": self.homepage,
            "estimated_size_mb": self.estimated_size_mb,
            "estimated_install_time_seconds": self.estimated_install_time_seconds,
            "documents": [
                {
                    "id": d.id, "title": d.title, "category": d.category,
                    "url": d.url, "filename": d.filename, "license": d.license,
                    "publisher": d.publisher, "size_bytes_estimate": d.size_bytes_estimate,
                    "required": d.required, "attribution": d.attribution,
                }
                for d in self.documents
            ],
        }
