import React from "react";
import { useAtomValue } from "jotai";
import { LightbulbIcon } from "lucide-react";
import { suggestionsAtom } from "@/core/suggestions/state";
import { cn } from "@/utils/cn";
import { PanelEmptyState } from "./empty-state";

export const SuggestionsPanel: React.FC = () => {
  const suggestions = useAtomValue(suggestionsAtom);

  if (!suggestions.length) {
    return (
      <PanelEmptyState
        icon={<LightbulbIcon />}
        title="No suggestions"
        description="There are currently no suggestions available."
      />
    );
  }

  return (
    <div className="flex flex-col gap-3 p-4">
      {suggestions.map((suggestion) => (
        <div
          key={suggestion.id}
          className={cn(
            "rounded-lg border p-4 transition-colors",
            "hover:border-border-hover",
            suggestion.type === "warning" &&
              "border-orange-500/50 bg-orange-500/10",
            suggestion.type === "idea" && "border-blue-500/50 bg-blue-500/10",
            suggestion.type === "tip" && "border-green-500/50 bg-green-500/10",
          )}
        >
          <div className="flex items-center gap-2">
            <LightbulbIcon className="h-4 w-4" />
            <h3 className="font-medium">{suggestion.title}</h3>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {suggestion.description}
          </p>
          <div className="mt-2 text-xs text-muted-foreground">
            {new Date(suggestion.timestamp).toLocaleString()}
          </div>
        </div>
      ))}
    </div>
  );
};
