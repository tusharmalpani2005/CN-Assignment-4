#!/usr/bin/env python3
"""
Generates plots for Assignment 4, Part 1 analysis from a CSV file.

Usage:
    python3 p1_plot.py <exp_name>

Where <exp_name> is either 'loss' or 'jitter'.

This script expects to find:
- 'reliability_loss.csv' if exp_name is 'loss'
- 'reliability_jitter.csv' if exp_name is 'jitter'
"""

import sys
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

def calculate_ci(data, confidence=0.90):
    """
    Calculates the mean and 90% confidence interval for a list of data points.
    
    Args:
        data (list): A list of numerical measurements (e.g., download times).
        confidence (float): The confidence level (default is 0.90).
        
    Returns:
        tuple: (mean, error_margin)
               The error_margin is the value to add/subtract from the mean
               for the confidence interval (e.g., mean ± error_margin).
    """
    a = 1.0 * np.array(data)
    n = len(a)
    if n == 0:
        return 0, 0
    
    mean = np.mean(a)
    
    # Calculate Standard Error of the Mean (SEM)
    sem = stats.sem(a)
    
    # Handle cases with no variance (n=1 or all values identical)
    # or empty data, where sem can be 0 or nan.
    if sem == 0 or np.isnan(sem):
        return mean, 0
        
    # Get the t-value for the given confidence level and degrees of freedom (n-1)
    # For a 90% CI, we look up the 95th percentile ( (1 + 0.90) / 2 )
    t_value = stats.t.ppf((1 + confidence) / 2., n - 1)
    
    # Confidence Interval error margin is t * SEM
    error_margin = t_value * sem
    
    return mean, error_margin

def main():
    """
    Main function to read CSV, process data, and generate plot.
    """
    
    # --- 1. Parse Command-Line Arguments ---
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <exp_name>", file=sys.stderr)
        print("  <exp_name> must be 'loss' or 'jitter'", file=sys.stderr)
        sys.exit(1)
        
    exp_name = sys.argv[1]

    # --- 2. Set file and plot parameters based on experiment name ---
    if exp_name == 'loss':
        input_file = 'reliability_loss.csv'
        x_column = 'loss'
        x_label = 'Packet Loss Rate (%)'
        title = 'Download Time vs. Packet Loss'
        output_file = 'loss_vs_time.png'
    elif exp_name == 'jitter':
        input_file = 'reliability_jitter.csv'
        x_column = 'jitter'
        x_label = 'Delay Jitter (ms)'
        title = 'Download Time vs. Delay Jitter'
        output_file = 'jitter_vs_time.png'
    else:
        print(f"Error: Unknown exp_name '{exp_name}'. Must be 'loss' or 'jitter'.", file=sys.stderr)
        sys.exit(1)

    # --- 3. Read and Process Data ---
    try:
        df = pd.read_csv(input_file)
    except FileNotFoundError:
        print(f"Error: Input file '{input_file}' not found.", file=sys.stderr)
        print("Please make sure the file is in the same directory.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error reading CSV file: {e}", file=sys.stderr)
        sys.exit(1)

    # Group data by the x-axis column (loss or jitter)
    grouped = df.groupby(x_column)
    
    x_values = []
    means = []
    errors = []

    # Calculate mean and CI for each group
    # We sort the keys to ensure the plot's x-axis is in order
    for x_val in sorted(grouped.groups.keys()):
        group = grouped.get_group(x_val)
        
        # Get the list of Time-To-Completion (ttc) values for this group
        ttc_data = group['ttc'].tolist()
        
        mean, error_margin = calculate_ci(ttc_data)
        
        x_values.append(x_val)
        means.append(mean)
        errors.append(error_margin)

    # --- 4. Generate the Plot ---
    plt.figure(figsize=(10, 6))
    
    # Plot the mean line with markers and error bars
    plt.errorbar(x_values, means, yerr=errors, linestyle='-',
                 marker='o', capsize=5, label='Mean Time (90% CI)')
    
    # --- Formatting ---
    plt.title(title, fontsize=16)
    plt.xlabel(x_label, fontsize=12)
    plt.ylabel('Download Time (seconds)', fontsize=12)
    
    # Set x-axis ticks to match the data points exactly
    plt.xticks(x_values) 
    
    plt.grid(True, linestyle='--', alpha=0.6)
    plt.legend()
    plt.tight_layout()
    
    # --- 5. Save the File ---
    plt.savefig(output_file)
    print(f"Plot successfully saved to {output_file}")
    plt.close()

if __name__ == "__main__":
    # Ensure you have the required libraries installed:
    # pip install pandas matplotlib scipy
    main()