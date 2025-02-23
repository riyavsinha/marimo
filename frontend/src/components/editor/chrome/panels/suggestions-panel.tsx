import React from "react";
import { useAtomValue } from "jotai";
import {
  MessageCircleQuestionIcon,
  MessageCircleWarningIcon,
  LightbulbIcon,
} from "lucide-react";
import { suggestionsAtom } from "@/core/suggestions/state";
import { cn } from "@/utils/cn";
import { PanelEmptyState } from "./empty-state";
import { useCellActions } from "@/core/cells/cells";
import { useLastFocusedCellId } from "@/core/cells/focus";

export const SuggestionsPanel: React.FC = () => {
  const suggestions = useAtomValue(suggestionsAtom);
  const { createNewCell } = useCellActions();
  const lastFocusedCellId = useLastFocusedCellId();

  if (!suggestions.length) {
    return (
      <PanelEmptyState
        icon={<LightbulbIcon />}
        title="No suggestions"
        description="There are currently no suggestions available."
      />
    );
  }

  const handleSuggestionClick = (title: string) => {
    createNewCell({
      code: `await mo.ai.agents.run_agent("${title}")`,
      before: false,
      cellId: lastFocusedCellId ?? "__end__",
    });
  };

  return (
    <div className="flex flex-col gap-3 p-4 overflow-y-auto">
      {suggestions.map((suggestion) => (
        <div
          key={suggestion.id}
          className={cn(
            "rounded-lg border p-4 transition-colors cursor-pointer",
            "hover:border-border-hover",
            suggestion.type === "prompt_warning" &&
              "border-orange-500/50 bg-orange-500/10",
            suggestion.type === "prompt_idea" &&
              "border-blue-500/50 bg-blue-500/10",
          )}
          onClick={() => handleSuggestionClick(suggestion.title)}
        >
          <div className="flex items-start gap-2">
            <div className="flex-shrink-0">
              {suggestion.type === "prompt_warning" ? (
                <MessageCircleWarningIcon className="h-6 w-6" />
              ) : (
                <MessageCircleQuestionIcon className="h-6 w-6" />
              )}
            </div>
            <div className="flex-1">
              <h3 className="font-medium">{suggestion.title}</h3>
              <p className="mt-1 text-sm text-muted-foreground">
                {suggestion.description}
              </p>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
};
